"""Bounded, no-retry HTTP client for the inspected Rust server contract."""

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from contextlib import contextmanager, nullcontext

import asyncio
import base64
import json
import re
import time

import httpx

from .config import Settings, format_bytes


class BridgeError(Exception):
    """A safe, user-visible error (never contains remote bodies or input text)."""


class HeavyGate:
    """Synchronous admission decisions: no await between check/increment or trip/release.

    A lost or unverified response may leave Rust spawn_blocking alive. Keep the
    circuit open until an administrator explicitly acknowledges recovery. This is
    process-local; retaining this object across config reloads is the caller's job.
    """

    def __init__(self):
        self.blocked = False
        self.active = 0
        self.epoch = None

    def configure(self, epoch: int):
        if self.epoch is not None and epoch > self.epoch:
            if self.active:
                raise BridgeError("重请求尚未退出，不能解除保护。")
            self.blocked = False
        if self.epoch is None or epoch > self.epoch:
            self.epoch = epoch

    @contextmanager
    def operation(self, limit: int):
        if self.blocked:
            raise BridgeError("重任务保护已熔断：请管理员确认 Rust 已排空或重启后递增“重任务恢复序号”。")
        if self.active >= limit:
            raise BridgeError("本插件重请求并发已满。")
        self.active += 1
        completion = [False]
        try:
            yield completion
        finally:
            if not completion[0]:
                self.blocked = True
            self.active -= 1


def json_body(payload: Any) -> bytes:
    """Exactly the bytes sent on the wire; no httpx-dependent JSON defaults."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise BridgeError("请求含无效 JSON 或 Unicode 数据。") from None


def image_kind(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise BridgeError("响应或输入不是支持的 PNG/GIF/JPEG/WebP 图片。")


class MemeClient:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.settings = settings
        self.gate = HeavyGate()
        self.http = httpx.AsyncClient(
            timeout=settings.timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=settings.concurrency, max_keepalive_connections=settings.concurrency),
            transport=transport,
        )
        self._metadata: Optional[List[Dict[str, Any]]] = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, limit: int, payload: Any = None) -> bytes:
        if not self.settings.base_url:
            raise BridgeError("尚未配置自部署服务 base_url。")
        body = self.bounded_body(payload) if payload is not None else None
        heavy = (
            method == "POST" and (path == "/tools/render_list" or re.fullmatch(r"/memes/[A-Za-z0-9_-]+", path))
        ) or (path.startswith("/memes/") and path.endswith("/preview"))
        return await self._read(method, self.settings.base_url + path, limit, body, bool(heavy))

    def bounded_body(self, payload: Any) -> bytes:
        body = json_body(payload)
        if len(body) > self.settings.max_request_bytes:
            raise BridgeError(
                "单次 HTTP JSON 请求体超过大小限制"
                f"（单次请求上限 {format_bytes(self.settings.max_request_bytes)}；含 UTF-8/Base64/JSON 封装）。"
            )
        return body

    async def _read(
        self, method: str, url: str, limit: int, body: Optional[bytes] = None, heavy: bool = False
    ) -> bytes:
        try:
            with self.gate.operation(self.settings.concurrency) if heavy else nullcontext(None) as completion:
                return await self._read_response(method, url, limit, body, completion)
        except httpx.TimeoutException:
            raise BridgeError("服务请求超时；未自动重试，服务端任务可能仍在运行。") from None
        except httpx.HTTPError:
            raise BridgeError("无法连接服务或读取失败，请检查地址与服务状态；未自动重试。") from None

    async def _read_response(self, method, url, limit, body, completion):
        async with self.http.stream(
            method, url, content=body, headers={"Accept-Encoding": "identity", "Content-Type": "application/json"}
        ) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise BridgeError("拒绝压缩响应，请配置服务或反向代理使用 identity 编码。")
            # Stream even error bodies; do not forward server text (it may echo user input).
            bound = limit if response.is_success else min(limit, 16_384)
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > bound:
                raise BridgeError(f"服务响应超过大小限制（上限 {format_bytes(bound)}）。")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16_384):
                if len(body) + len(chunk) > bound:
                    raise BridgeError(f"服务响应超过大小限制（上限 {format_bytes(bound)}）。")
                body.extend(chunk)
            parsed = None
            if completion is not None or not response.is_success:
                try:
                    parsed = json.loads(body)
                except (ValueError, UnicodeError):
                    pass
            code = parsed.get("code") if isinstance(parsed, dict) and type(parsed.get("code")) is int else None
            if completion is not None:
                # Native Rust returns a complete image_id only after rendering and
                # storage. Unknown/proxy errors, truncated or malformed responses
                # do NOT establish completion, even if headers were received.
                completion[0] = bool(
                    response.is_success
                    and isinstance(parsed, dict)
                    and isinstance(parsed.get("image_id"), str)
                    and re.fullmatch(r"[a-f0-9]{32}", parsed["image_id"])
                ) or (not response.is_success and code in {410, 420, 510, 520, 530, 540, 550, 551, 560, 570})
            if not response.is_success:
                hints = {
                    530: "服务缺少模板素材",
                    550: "图片数量不符",
                    551: "文字数量不符",
                    560: "文字过长",
                    510: "图片解码失败",
                    570: "模板拒绝该输入",
                }
                hint = hints.get(code, "服务请求失败")
                raise BridgeError(f"{hint}（HTTP {response.status_code}，错误码 {code or '无'}）；未自动重试。")
            return bytes(body)

    async def json_request(self, method: str, path: str, payload: Any = None) -> Any:
        raw = await self.request(method, path, self.settings.max_metadata_bytes, payload)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError("服务返回无效 JSON，请核对 Rust 服务版本。") from None

    async def metadata(self, force: bool = False) -> List[Dict[str, Any]]:
        async with self._lock:
            if not force and self._metadata is not None and time.monotonic() < self._expires:
                return self._metadata
            data = await self.json_request("GET", "/meme/infos")
            try:
                if not isinstance(data, list) or len(data) > 10000:
                    raise ValueError
                keys = set()
                for item in data:
                    key, params = item["key"], item["params"]
                    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) or key in keys:
                        raise ValueError
                    keys.add(key)
                    if (
                        not isinstance(item["keywords"], list)
                        or len(item["keywords"]) > 32
                        or not all(isinstance(x, str) and len(x) <= 128 for x in item["keywords"])
                    ):
                        raise ValueError
                    for name in ("images", "texts"):
                        lo, hi = params[f"min_{name}"], params[f"max_{name}"]
                        if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi <= 255:
                            raise ValueError
                if sum(len(item["keywords"]) for item in data) > 20_000:
                    raise ValueError
                self._metadata = data
                self._expires = time.monotonic() + self.settings.cache_ttl
                return data
            except (KeyError, TypeError, ValueError):
                raise BridgeError("模板元数据格式不兼容，请核对 Rust 服务版本。") from None

    def disabled(self, item: Dict[str, Any]) -> bool:
        return bool(set(self.settings.disabled_templates) & {item["key"], *item["keywords"]})

    async def resolve(self, name: str) -> Dict[str, Any]:
        matches = [x for x in await self.metadata() if name == x["key"] or name in x["keywords"]]
        if not matches:
            raise BridgeError("未找到模板，请使用 /表情列表 查看精确 key 或关键词。")
        if len(matches) > 1:
            raise BridgeError(
                "模板名重名，请使用精确 key："
                + "、".join(x["key"] + ("[禁用]" if self.disabled(x) else "") for x in matches[:20])
            )
        if self.disabled(matches[0]):
            raise BridgeError("该模板已禁用。")
        return matches[0]

    async def result_image(self, result: Any) -> Tuple[bytes, str]:
        if not isinstance(result, dict) or not isinstance(result.get("image_id"), str):
            raise BridgeError("服务未返回 image_id。")
        image_id = result["image_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", image_id):
            raise BridgeError("服务 image_id 格式不兼容。")
        data = await self.request("GET", "/image/" + image_id, self.settings.max_output_bytes)
        return data, image_kind(data)

    async def preview(self, key: str) -> Tuple[bytes, str]:
        return await self.result_image(await self.json_request("GET", f"/memes/{quote(key, safe='')}/preview"))

    async def generate(self, item: Dict[str, Any], texts: List[str], images: List[bytes]) -> Tuple[bytes, str]:
        params = item["params"]
        if self.disabled(item):
            raise BridgeError("该模板已禁用。")
        for name, values in (("images", images), ("texts", texts)):
            if not params[f"min_{name}"] <= len(values) <= params[f"max_{name}"]:
                label = "图片" if name == "images" else "文字"
                raise BridgeError(
                    f"{label}数量要求 {params[f'min_{name}']}–{params[f'max_{name}']}，实际 {len(values)}。"
                )
        if len(images) > self.settings.max_images or any(len(x) > self.settings.max_input_bytes for x in images):
            raise BridgeError(f"输入图片数量或大小超过限制（单图上限 {format_bytes(self.settings.max_input_bytes)}）。")
        if any(len(x) > self.settings.max_text_chars for x in texts):
            raise BridgeError("输入文字超过长度限制。")
        if sum(map(len, images)) > self.settings.max_total_input_bytes:
            raise BridgeError(
                f"输入图片原始字节总量超过限制（全部输入上限 {format_bytes(self.settings.max_total_input_bytes)}）。"
            )
        if sum(map(len, texts)) > self.settings.max_total_text_chars:
            raise BridgeError("输入文字总字符数超过限制。")
        try:
            text_bytes = sum(len(text.encode("utf-8")) for text in texts)
        except UnicodeError:
            raise BridgeError("文字含无效 Unicode 数据。") from None
        if text_bytes > self.settings.max_total_text_bytes:
            raise BridgeError(
                f"输入文字 UTF-8 总字节数超过限制（文字总量上限 {format_bytes(self.settings.max_total_text_bytes)}）。"
            )
        # Preflight *every* upload and the final request before upload #1.
        # Uploads are separate HTTP requests, not one combined image payload.
        final = {
            "images": [{"name": f"image{i + 1}", "id": "0" * 32} for i in range(len(images))],
            "texts": texts,
            "options": {},
        }
        final_size = len(self.bounded_body(final))
        upload_sizes = [len(json_body({"type": "data", "data": ""})) + 4 * ((len(image) + 2) // 3) for image in images]
        if any(size > self.settings.max_request_bytes for size in upload_sizes):
            raise BridgeError(
                "图片 Base64 与 JSON 封装后的单次上传请求超过大小限制"
                f"（单次请求上限 {format_bytes(self.settings.max_request_bytes)}）。"
            )
        if sum(upload_sizes) + final_size > self.settings.max_operation_request_bytes:
            raise BridgeError(
                "逐图上传及生成 JSON 请求体累计超过大小限制"
                f"（整次累计上限 {format_bytes(self.settings.max_operation_request_bytes)}）。"
            )
        for image in images:
            image_kind(image)
        if self.gate.blocked:
            raise BridgeError("重任务保护已熔断；未上传图片，请管理员确认服务后恢复。")
        uploaded = []
        for index, image in enumerate(images):
            image_kind(image)
            result = await self.json_request(
                "POST", "/image/upload", {"type": "data", "data": base64.b64encode(image).decode("ascii")}
            )
            if not isinstance(result, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(result.get("image_id", ""))):
                raise BridgeError("上传响应 image_id 格式不兼容。")
            uploaded.append({"name": f"image{index + 1}", "id": result["image_id"]})
        result = await self.json_request(
            "POST", f"/memes/{quote(item['key'], safe='')}", {"images": uploaded, "texts": texts, "options": {}}
        )
        return await self.result_image(result)
