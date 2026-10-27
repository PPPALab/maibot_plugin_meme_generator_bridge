"""Parse actual SDK message segments, never nickname-derived mentions or user URLs."""

from typing import Any, Dict, List, Tuple

import asyncio
import base64
import binascii
import hashlib
import re
import shlex

from .client import BridgeError, image_kind
from .config import Settings


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
        raise BridgeError("输入图片超过大小限制。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise BridgeError("SDK 图片 Base64 无效。") from None
    if len(data) > settings.max_input_bytes:
        raise BridgeError("输入图片超过大小限制。")
    image_kind(data)
    return data


async def collect_images(
    ctx: Any, client: Any, message: Dict[str, Any], stream_id: str, avatars: Any = None
) -> List[bytes]:
    raw = segments(message)
    if any(x.get("type") in {"reply", "forward"} for x in raw) or message.get("reply_to"):
        raise BridgeError("本版不支持引用图或转发图，请将图片附在当前命令消息中。")
    media = [x for x in raw if x.get("type") in {"image", "emoji", "at"}]
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
            raise BridgeError("输入图片原始字节总量超过限制。")
    return images
