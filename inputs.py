"""Parse actual SDK message segments, never nickname-derived mentions or user URLs."""

from typing import Any, Dict, List, Tuple

import asyncio
import base64
import binascii
import hashlib
import re
import shlex

from .client import BridgeError, image_kind
from .config import Settings, format_bytes


def segments(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = message.get("raw_message", [])
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        raise BridgeError("当前消息结构不兼容。")
    return raw


def parse_command(text: str, settings: Settings) -> Tuple[str, List[str]]:
    if len(text) > settings.max_command_chars:
        raise BridgeError("命令过长。")
    try:
        parts = shlex.split(text)
    except ValueError:
        raise BridgeError('引号不匹配；用法：/做表情 模板名 "文字1" "文字2"。') from None
    if not parts:
        raise BridgeError("缺少命令。")
    return parts[0], parts[1:]


def command_text(message: Dict[str, Any], default: str) -> str:
    raw = segments(message)
    # processed_plain_text may contain descriptions of images and nicknames;
    # reconstruct only text segments when available, preserving quoted texts.
    texts = [x["data"] if x.get("type") == "text" and isinstance(x.get("data"), str) else " " for x in raw]
    return "".join(texts).strip() if texts else default


def decode_image(encoded: Any, settings: Settings) -> bytes:
    if not isinstance(encoded, str) or not encoded:
        raise BridgeError("当前图片无可用 SDK 二进制数据；不下载消息中的 URL，请重新附图。")
    if len(encoded) > 4 * ((settings.max_input_bytes + 2) // 3):
        raise BridgeError(f"输入图片超过大小限制（单图上限 {format_bytes(settings.max_input_bytes)}）。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise BridgeError("SDK 图片 Base64 无效。") from None
    if len(data) > settings.max_input_bytes:
        raise BridgeError(f"输入图片超过大小限制（单图上限 {format_bytes(settings.max_input_bytes)}）。")
    image_kind(data)
    return data


def _reply_target(message: Dict[str, Any]) -> str:
    """Quote target from a structured reply segment, falling back to reply_to."""
    for item in segments(message):
        if item.get("type") != "reply":
            continue
        data = item.get("data")
        target = data.get("target_message_id") if isinstance(data, dict) else data
        if isinstance(target, str) and target:
            return target
    reply_to = message.get("reply_to")
    return reply_to if isinstance(reply_to, str) and reply_to else ""


async def quoted_images(ctx: Any, client: Any, message: Dict[str, Any], stream_id: str, target: str) -> List[bytes]:
    """Fetch one referenced message via the official SDK; never list history."""
    settings = client.settings
    try:
        result = await asyncio.wait_for(
            ctx.message.get_by_id(message_id=target, chat_id=stream_id, include_binary_data=True),
            settings.timeout,
        )
    except asyncio.TimeoutError:
        raise BridgeError("读取引用图片超时，请重新发送或直接附图。") from None
    except BridgeError:
        raise
    except Exception:
        raise BridgeError("引用的图片已不可用，请重新发送或直接附图。") from None
    # SDK 2.16 unwraps message.get_by_id to the MessageDict (or None).
    # Accept the raw Host envelope too, but never ignore an explicit failure.
    fetched = None
    if isinstance(result, dict):
        if "success" in result:
            if result["success"] is True:
                fetched = result.get("message")
        else:
            fetched = result
    if not isinstance(fetched, dict):
        raise BridgeError("引用的图片已不可用，请重新发送或直接附图。")
    if fetched.get("session_id") != stream_id:
        raise BridgeError("引用的消息不在当前会话，已拒绝作为输入。")
    encoded = []
    for item in segments(fetched):
        if item.get("type") not in {"image", "emoji"}:
            continue
        data = item.get("binary_data_base64")
        if not isinstance(data, str) or not data:
            raise BridgeError("引用的图片已不可用，请重新发送或直接附图。")
        encoded.append(data)
        if len(encoded) > settings.max_images:
            raise BridgeError("引用图片数量超过限制，请减少或直接附图。")
    total = 0
    images = []
    for data in encoded:
        try:
            decoded = decode_image(data, settings)
        except BridgeError as exc:
            raise BridgeError(f"引用的图片不可用：{exc}") from None
        total += len(decoded)
        if total > settings.max_total_input_bytes:
            raise BridgeError(
                f"引用图片原始字节总量超过限制（全部输入上限 {format_bytes(settings.max_total_input_bytes)}），"
                "请减少或直接附图。"
            )
        images.append(decoded)
    return images


async def collect_images(
    ctx: Any,
    client: Any,
    message: Dict[str, Any],
    stream_id: str,
    avatars: Any = None,
    *,
    max_images_needed: int | None = None,
) -> List[bytes]:
    raw = segments(message)
    if any(x.get("type") == "forward" for x in raw):
        raise BridgeError("本版不支持转发图，请将图片附在当前命令消息或引用一条图片消息。")
    media = [x for x in raw if x.get("type") in {"image", "emoji", "at"}]
    # Current attachments win. Otherwise a quote wins over every @, regardless
    # of position or identity; no quote means all @ remain avatar inputs.
    if not any(x.get("type") in {"image", "emoji"} for x in media):
        target = _reply_target(message)
        if target and client.settings.allow_quoted_images and max_images_needed != 0:
            return await quoted_images(ctx, client, message, stream_id, target)
    if not media:
        return []
    if len(media) > client.settings.max_images:
        raise BridgeError("输入图片或 @ 数量超过限制。")
    images = []
    seen = set()
    for item in media:
        if item["type"] == "at":
            data = item.get("data")
            target = data.get("target_user_id", "") if isinstance(data, dict) else ""
            if not isinstance(target, str) or not re.fullmatch(r"[0-9]{5,20}", target):
                raise BridgeError("仅支持结构化真实 QQ @ID；不支持全体成员或昵称猜测。")
            if ("at", target) in seen:
                continue
            seen.add(("at", target))
            if avatars is not None and not avatars.settings.enabled:
                raise BridgeError("管理员已关闭 @头像获取，请直接附图。")

            async def fetch_avatar():
                avatar = await ctx.chat.get_avatar(
                    platform="qq",
                    target_id=target,
                    target_type="user",
                    account_id=message.get("account_id") or "",
                    scope=message.get("scope") or "",
                )
                if (
                    not isinstance(avatar, dict)
                    or avatar.get("status") != "available"
                    or not isinstance(avatar.get("url"), str)
                ):
                    raise BridgeError("当前适配器不支持头像或该头像不可用；请直接附图。")
                if avatars is None:
                    raise BridgeError("官方 SDK 仅提供头像 URL；头像下载器未启用，请直接附图。")
                return await avatars.get(avatar["url"], target)

            try:
                images.append(
                    await asyncio.wait_for(fetch_avatar(), avatars.settings.timeout if avatars is not None else 8)
                )
            except asyncio.TimeoutError:
                raise BridgeError("头像查询或下载超时，请直接附图。") from None
        else:
            data = decode_image(item.get("binary_data_base64"), client.settings)
            identity = ("image", hashlib.sha256(data).digest())
            if identity not in seen:
                images.append(data)
                seen.add(identity)
        if sum(map(len, images)) > client.settings.max_total_input_bytes:
            raise BridgeError(
                f"输入图片原始字节总量超过限制（全部输入上限 {format_bytes(client.settings.max_total_input_bytes)}）。"
            )
    return images
