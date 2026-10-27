"""Deterministic commands/keywords; no AI intent inference or Docker management."""

from collections import OrderedDict
from typing import Any, Dict, List, Set

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

import asyncio
import base64
import re
import time

from .client import BridgeError, HeavyGate, MemeClient
from .config import BridgeConfig
from .inputs import collect_images, command_text, parse_command, segments
from .keywords import KeywordIndex
from .media import AvatarDownloader
from .menu import render_index


HELP = (
    "/表情帮助 [关键词]\n/表情列表 [关键词]\n/表情详情 模板名\n/表情预览 模板名\n"
    '/做表情 模板名 "文字1" "文字2"\n'
    "无参数发一张全量关键词索引图；带关键词查看要求与示例。"
    "也可发送中文关键词 + 真实 @、当前附图或同一会话引用图；多段文字加引号。不支持转发图或用户 URL。"
)


def index_text(items: List[Dict[str, Any]], total: int) -> str:
    """One-line-per-keyword text fallback; truncated, never a page-by-page dump."""
    names = "、".join(min(x["keywords"], key=len) if x["keywords"] else x["key"] for x in items)
    if len(names) > 1500:
        names = names[:1500] + "……"
    return names + f"\n共 {total} 个，详情用 /表情帮助 关键词"


# Host _build_command_executor reads this both from a returned tuple and from
# Command metadata if the Runner reports an exception/non-tuple result.
CONSUMED = 1


class MemeBridgePlugin(MaiBotPlugin):
    config_model = BridgeConfig

    def __init__(self) -> None:
        super().__init__()
        self.client = None
        self._tasks: Set[asyncio.Task] = set()
        self._cooldowns: OrderedDict = OrderedDict()
        self._media: OrderedDict = OrderedDict()
        self._pending = 0
        self._closing = True
        self._index = KeywordIndex([], [])
        self._refresh_task = None
        self.avatars = None
        self._keyword_jobs: Set[asyncio.Task] = set()
        self._keyword_bytes = 0
        self._busy_notice = None
        self._reload_lock = asyncio.Lock()
        self._heavy_gate = HeavyGate()
        self._cleanup_task = None
        self._clock = time.monotonic
        self._generation = 0

    async def on_load(self) -> None:
        self._generation += 1
        generation = self._generation
        settings = self.config.plugin
        self._heavy_gate.configure(settings.recovery_epoch)
        self.client = MemeClient(settings)
        self.client.gate = self._heavy_gate
        self._slots = asyncio.Semaphore(settings.concurrency)
        self._closing = False
        self.avatars = AvatarDownloader(self.config.avatar, settings.max_input_bytes)
        self._index = KeywordIndex([], [])
        if settings.enabled and self.config.keywords.enabled:
            await self._refresh_keywords()
            if self._closing or generation != self._generation:
                return
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(settings.cache_cleanup_interval))

    def _purge_media(self):
        now = self._clock()
        # No await in inspection/removal: a tick cannot delete a replacement value.
        for key, (expires, _) in list(self._media.items()):
            if expires <= now:
                del self._media[key]

    async def _cleanup_loop(self, interval):
        while True:
            await asyncio.sleep(interval)
            self._purge_media()
            self.avatars.purge_expired()

    def _set_index(self, items):
        # Build off to the side and swap once; no partially refreshed registrations.
        self._index = KeywordIndex(items, self.config.keywords.exclusions)

    async def _refresh_keywords(self):
        client, generation = self.client, self._generation
        try:
            items = await client.metadata(force=True)
            if not self._closing and generation == self._generation and client is self.client:
                self._set_index(items)
        except BridgeError:
            self.ctx.logger.warning("表情关键词刷新失败；保留当前内存快照，请用 /表情列表 检查服务。")

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(self.config.keywords.refresh_seconds)
            await self._refresh_keywords()

    async def on_unload(self) -> None:
        self._closing = True
        self._generation += 1
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        tasks = [task for task in self._tasks | self._keyword_jobs if task is not asyncio.current_task()]
        if self._busy_notice is not None:
            tasks.append(self._busy_notice)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.close()
        if self.avatars is not None:
            await self.avatars.close()
        self._index = KeywordIndex([], [])
        self._cooldowns.clear()
        self._media.clear()
        self._keyword_jobs.clear()
        self._keyword_bytes = 0

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope == "self":
            # Runner can deliver overlapping config RPCs. Finish cancellation/close
            # before replacing state, and reapply this RPC's payload after waiting.
            async with self._reload_lock:
                await self.on_unload()
                if config_data:
                    self.set_plugin_config(config_data)
                await self.on_load()

    @HookHandler("chat.receive.before_process", name="meme_keyword_trigger", mode=HookMode.BLOCKING)
    async def handle_keyword(self, message: Any = None, **kwargs: Any):
        if (
            self._closing
            or self.client is None
            or not isinstance(message, dict)
            or message.get("platform") != "qq"
            or not self.config.keywords.enabled
        ):
            return None
        info = message.get("message_info") or {}
        group_id = str((info.get("group_info") or {}).get("group_id", ""))
        if not self.client.settings.enabled or not self.client.settings.allows_chat(group_id):
            return None
        match = self._index.match(message)
        if match is None:
            return None
        # From here on this exact keyword owns the message, including every refusal/error.
        _, converted = match
        stream_id = message.get("session_id", "")
        try:
            info = message.get("message_info") or {}
            group = info.get("group_info") or {}
            user = info.get("user_info") or {}
            size = sum(len(item.get("binary_data_base64", "")) for item in segments(converted))
            if (
                len(self._keyword_jobs) >= self.client.settings.concurrency + self.client.settings.max_waiters
                or self._keyword_bytes + size > 16_000_000
            ):
                if self._busy_notice is None or self._busy_notice.done():
                    self._busy_notice = asyncio.create_task(
                        self._report_error("服务繁忙，关键词命令等待队列已满或附图过大。", stream_id)
                    )
            else:
                self._keyword_bytes += size
                task = asyncio.create_task(self._run_keyword(size, converted, stream_id, group, user))
                self._keyword_jobs.add(task)
                task.add_done_callback(self._keyword_jobs.discard)
        except Exception as exc:
            self.ctx.logger.warning("关键词命令处理失败 type=%s", type(exc).__name__)
        # The abort decision is final before any SDK query, queue wait or generation
        # starts. Background work never decides whether the Host should continue.
        return {"action": "abort"}

    async def _run_keyword(self, size, message, stream_id, group, user):
        try:
            await self.handle_command(
                message=message,
                stream_id=stream_id,
                platform="qq",
                user_id=str(user.get("user_id", "")),
                group_id=str(group.get("group_id", "")),
                _keyword=True,
            )
        except Exception as exc:
            self.ctx.logger.warning("关键词任务失败 type=%s", type(exc).__name__)
        finally:
            self._keyword_bytes -= size

    @HookHandler("chat.command.before_execute", name="meme_current_media", mode=HookMode.BLOCKING)
    async def capture_current_media(self, message: Any = None, command_name: str = "", **kwargs: Any):
        """The host strips binaries from Command kwargs, but this blocking Hook has them.

        Never query history or mutate the host message. Keep at most one command's
        images per pending slot, bound each encoded image, expire after 30 seconds.
        """
        if self._closing or self.client is None or command_name != "meme_bridge" or not isinstance(message, dict):
            return None
        settings = self.client.settings
        if not settings.enabled or message.get("platform") != "qq":
            return None
        if not re.match(r"^/做表情(?:\s|$)", command_text(message, "")):
            return None
        info = message.get("message_info", {})
        group = info.get("group_info") or {}
        group_id = str(group.get("group_id", ""))
        if not settings.allows_chat(group_id):
            return None
        now = self._clock()
        self._purge_media()
        key = (message.get("session_id"), message.get("message_id"))
        if not all(isinstance(x, str) and x for x in key):
            return None
        media = []
        for index, item in enumerate(segments(message)):
            if item.get("type") in {"image", "emoji"}:
                encoded = item.get("binary_data_base64")
                if not isinstance(encoded, str) or len(encoded) > 4 * ((settings.max_input_bytes + 2) // 3):
                    return None
                media.append((index, item.get("type"), item.get("hash"), encoded))
                if len(media) > settings.max_images:
                    return None
        if media:
            # 16 MB encoded cache budget, independent of configurable queue capacity.
            size = sum(len(item[3]) for item in media)
            if size > 16_000_000:
                return None
            while (
                self._media
                and sum(len(item[3]) for _, items in self._media.values() for item in items) + size > 16_000_000
            ):
                self._media.popitem(last=False)
            self._media[key] = (now + 30, media)
            while len(self._media) > settings.concurrency + settings.max_waiters:
                self._media.popitem(last=False)
        return None

    def _attach_current_media(self, message: Dict[str, Any], stream_id: str) -> Dict[str, Any]:
        cached = self._media.pop((stream_id, message.get("message_id")), None)
        if cached is None or cached[0] <= self._clock():
            return message
        raw = [dict(item) for item in segments(message)]
        for index, kind, image_hash, encoded in cached[1]:
            if index >= len(raw) or raw[index].get("type") != kind or raw[index].get("hash") != image_hash:
                raise BridgeError("当前消息图片结构不一致，已拒绝。")
            raw[index]["binary_data_base64"] = encoded
        return {**message, "raw_message": raw}

    async def _execute_keyword(self, text: str, message: Dict[str, Any], stream_id: str) -> None:
        # before_process aborts before Host get_or_create_session. Use only the
        # official SDK, after admission and inside the operation deadline. Never
        # derive a session ID or trust a different route returned by the Host.
        info = message.get("message_info") or {}
        group_id = str((info.get("group_info") or {}).get("group_id", ""))
        user_id = str((info.get("user_info") or {}).get("user_id", ""))
        route = {
            "platform": "qq",
            "chat_type": "group" if group_id else "private",
            "user_id": user_id,
            "group_id": group_id,
            "account_id": message.get("account_id") or "",
            "scope": message.get("scope") or "",
        }
        result = await self.ctx.chat.open_session(**route)
        fields = ("platform", "chat_type", "group_id" if group_id else "user_id", "account_id", "scope")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or result.get("session_id") != stream_id
            or any((result.get(field) or "") != route[field] for field in fields)
        ):
            raise BridgeError("宿主未能确认当前聊天流，已停止生成；请检查宿主与适配器路由。")
        await self._execute(text, message, stream_id)

    async def _execute(self, text: str, message: Dict[str, Any], stream_id: str) -> None:
        settings = self.client.settings
        command, args = parse_command(command_text(message, text), settings)
        if command in {"/表情帮助", "/表情列表"}:
            if len(args) > 1:
                raise BridgeError("用法：/表情帮助 [关键词]")
            try:
                all_items = await self.client.metadata(force=True)
            except BridgeError as exc:
                raise BridgeError(HELP + "\n菜单不可用：" + str(exc)) from None
            self._set_index(all_items)
            items = sorted([x for x in all_items if not self.client.disabled(x)], key=lambda x: x["key"])
            total = len(items)
            if args:
                try:
                    detail = await self.client.resolve(args[0])
                except BridgeError as exc:
                    # Unresolved lookups stay consumed; suggest candidates, never auto-pick.
                    if "未找到" not in str(exc):
                        raise
                    hints = [
                        min(x["keywords"], key=len) if x["keywords"] else x["key"]
                        for x in items
                        if args[0] in x["key"] or any(args[0] in name for name in x["keywords"])
                    ][:5]
                    raise BridgeError("未找到该表情" + (f"；相近候选：{'、'.join(hints)}" if hints else "")) from None
                params = detail["params"]
                keyword = min(detail["keywords"], key=len) if detail["keywords"] else detail["key"]
                if not params["max_images"]:
                    example = f'{keyword} "文字"' if params["max_texts"] else keyword
                elif params["max_texts"]:
                    example = f'{keyword} @目标 "文字"'
                else:
                    pictures = " [附图 1] [附图 2]" if params["max_images"] >= 2 else " [附图 1]"
                    example = keyword + " @目标" + pictures
                reply = (
                    f"{keyword}（{detail['key']}）\n"
                    f"需要：图片 {params['min_images']}–{params['max_images']} 张；文字 {params['min_texts']}–{params['max_texts']} 段\n"
                    f"用法：{example}\n"
                    f"预览：/表情预览 {detail['key']}   详情：/表情详情 {detail['key']}"
                )
            elif self.config.menu.image_enabled and items:
                try:
                    data = await asyncio.to_thread(
                        render_index,
                        items,
                        total,
                        self.config.menu.max_pixels,
                        settings.max_output_bytes,
                    )
                    if not await self.ctx.send.image(base64.b64encode(data).decode("ascii"), stream_id):
                        raise BridgeError("索引图已绘制，但宿主未确认发送；未重试。")
                    return
                except BridgeError as exc:
                    if not self.config.menu.text_fallback:
                        raise
                    reply = index_text(items, total) + "\n索引图失败，已用文字清单：" + str(exc)
                except Exception as exc:
                    self.ctx.logger.warning("索引图处理失败 type=%s", type(exc).__name__)
                    if not self.config.menu.text_fallback:
                        raise BridgeError("索引图读取或发送失败，请检查服务和宿主 SDK。") from None
                    reply = index_text(items, total) + "\n索引图读取或发送失败，已用文字清单；请检查服务和宿主 SDK。"
            else:
                reply = index_text(items, total)
        elif command in {"/表情详情", "/表情预览", "/做表情"}:
            if not args or (command != "/做表情" and len(args) != 1):
                raise BridgeError(f"用法：{command} 模板名" + (' "文字1" "文字2"' if command == "/做表情" else ""))
            item = await self.client.resolve(args[0])
            if command == "/表情详情":
                params = item["params"]
                reply = (
                    f"{item['key']}：{' / '.join(item['keywords'])[:300]}\n"
                    f"图片 {params['min_images']}–{params['max_images']}；文字 {params['min_texts']}–{params['max_texts']}\n"
                    "本版使用默认 options={}；文字须显式提供，不自动填充 default_texts。"
                )
            else:
                if command == "/表情预览":
                    data, kind = await self.client.preview(item["key"])
                else:
                    images = await collect_images(
                        self.ctx,
                        self.client,
                        message,
                        stream_id,
                        self.avatars,
                        max_images_needed=item["params"]["max_images"],
                    )
                    data, kind = await self.client.generate(item, args[1:], images)
                encoded = base64.b64encode(data).decode("ascii")
                sent = await (
                    self.ctx.send.emoji(encoded, stream_id)
                    if kind == "gif"
                    else self.ctx.send.image(encoded, stream_id)
                )
                if not sent:
                    raise BridgeError("图片已生成，但宿主发送失败；请检查适配器及 RPC 大小限制。")
                return
        else:
            raise BridgeError("未知命令。" + HELP)
        if not await self.ctx.send.text(reply, stream_id):
            raise BridgeError("宿主文本发送失败。")

    @Command(
        "meme_bridge",
        description="自部署 Rust 表情服务桥接",
        pattern=r"^/(?:表情帮助|表情列表|表情详情|表情预览|做表情)(?:\s|$)",
        intercept_message_level=CONSUMED,
    )
    async def handle_command(
        self,
        text: str = "",
        message: Any = None,
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        _keyword: bool = False,
        **kwargs: Any,
    ):
        if self._closing or self.client is None:
            return False, None, CONSUMED
        settings = self.client.settings
        if not settings.enabled or platform != "qq":
            return False, None, CONSUMED
        if not settings.allows_chat(group_id):
            return False, None, CONSUMED
        if not stream_id or not user_id or not isinstance(message, dict):
            return False, None, CONSUMED
        task = asyncio.current_task()
        admitted = False
        acquired = False
        self._tasks.add(task)
        try:
            segments(message)
            message = self._attach_current_media(message, stream_id)
            now = self._clock()
            key = (platform, user_id)
            if now - self._cooldowns.get(key, -float("inf")) < settings.cooldown:
                raise BridgeError("命令冷却中，请稍后再试。")
            if self._pending >= settings.concurrency + settings.max_waiters:
                raise BridgeError("服务繁忙，等待队列已满。")
            self._cooldowns[key] = now
            self._cooldowns.move_to_end(key)
            while len(self._cooldowns) > 4096:
                self._cooldowns.popitem(last=False)
            self._pending += 1
            admitted = True
            try:
                await asyncio.wait_for(self._slots.acquire(), settings.queue_timeout)
                acquired = True
            except asyncio.TimeoutError:
                raise BridgeError("等待服务超时，请稍后再试。") from None
            if self._closing:
                # Python 3.10 wait_for can complete an acquire concurrently with cancellation.
                # Never start generation after unload has begun, even in that race.
                raise asyncio.CancelledError
            try:
                execute = self._execute_keyword if _keyword else self._execute
                await asyncio.wait_for(execute(text, message, stream_id), settings.operation_timeout)
            except asyncio.TimeoutError:
                raise BridgeError("命令总耗时超限；未重试，服务端生成任务可能仍在运行。") from None
            return True, None, CONSUMED
        except BridgeError as exc:
            await self._report_error(str(exc), stream_id)
            return False, None, CONSUMED
        except Exception as exc:
            # No exception body/traceback: SDK and service exceptions can contain user text or URLs.
            self.ctx.logger.warning("meme bridge failure type=%s", type(exc).__name__)
            await self._report_error("宿主能力或插件处理失败，请检查版本、权限与适配器；未自动重试。", stream_id)
            return False, None, CONSUMED
        finally:
            if acquired:
                self._slots.release()
            if admitted:
                self._pending -= 1
            self._tasks.discard(task)

    async def _report_error(self, text: str, stream_id: str) -> None:
        """At most one error-send attempt; a broken sender must not unconsume a command."""
        try:
            await asyncio.wait_for(self.ctx.send.text(text, stream_id), timeout=3)
        except Exception as exc:
            self.ctx.logger.warning("meme bridge error-send failure type=%s", type(exc).__name__)


def create_plugin() -> MemeBridgePlugin:
    return MemeBridgePlugin()
