"""Pre-rendered menu artwork. Runtime requests never draw or load system fonts."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Tuple

import hashlib
import json
import struct

from .client import BridgeError
from .config import format_bytes, format_pixels


BUNDLE_DIR = Path(__file__).with_name("assets")
TemplateSet = FrozenSet[Tuple[str, Tuple[str, ...]]]


def template_set(items: List[Dict[str, Any]]) -> TemplateSet:
    """Match actual visible templates, ignoring metadata and keyword order."""
    return frozenset((item["key"], tuple(sorted(item["keywords"]))) for item in items)


@dataclass(frozen=True)
class BundledIndex:
    data: bytes
    templates: TemplateSet
    width: int
    height: int

    def image_for(self, items: List[Dict[str, Any]], total: int, max_pixels: int, max_bytes: int) -> bytes:
        if len(items) != total or total != len(self.templates) or template_set(items) != self.templates:
            raise BridgeError("当前可用模板与随包菜单不同，无法使用预绘菜单图。")
        if self.width * self.height > max_pixels:
            raise BridgeError(f"随包菜单图超过像素限制（上限 {format_pixels(max_pixels)}）。")
        if len(self.data) > max_bytes:
            raise BridgeError(f"随包菜单图超过输出字节限制（上限 {format_bytes(max_bytes)}）。")
        return self.data


def _read_asset(name: str, limit: int) -> bytes:
    with (BUNDLE_DIR / name).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Bundled menu asset exceeds its budget")
    return data


@lru_cache(maxsize=1)
def load_bundled_index() -> BundledIndex:
    """Read once at plugin load, on a joined filesystem worker; cache immutable data."""
    try:
        manifest = json.loads(_read_asset("menu-v0.2.3.json", 200_000))
        data = _read_asset("menu-v0.2.3.png", 1_000_000)
        if manifest["schema_version"] != 1 or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            raise ValueError("Bundled menu digest mismatch")
        if data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise ValueError("Bundled menu is not a PNG")
        width, height = struct.unpack(">II", data[16:24])
        if (width, height) != (manifest["width"], manifest["height"]) or not 0 < width * height <= 8_000_000:
            raise ValueError("Bundled menu dimensions mismatch")
        templates = template_set(manifest["templates"])
        if not templates or len(templates) != len(manifest["templates"]):
            raise ValueError("Bundled menu template manifest is invalid")
        return BundledIndex(data, templates, width, height)
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        raise BridgeError("随包菜单文件缺失或损坏，请重新安装插件。") from None
