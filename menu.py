"""Bounded offline keyword index. All upstream strings are literal drawn text, not HTML."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

from .client import BridgeError
from .config import format_bytes, format_pixels


MAX_ITEMS = 10_000
CELL_FONT_SIZE = 20
FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/unifont/unifont.otf",
    "/usr/share/fonts/truetype/unifont/unifont.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
)


@lru_cache(maxsize=8)
def _font_and_missing_glyph(font_path: str, size: int):
    """Load one font per process and render its .notdef box once."""
    font = ImageFont.truetype(font_path, size)
    probe = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(probe).text((0, 0), "\ue000", font=font, fill=255)
    return font, probe.tobytes()


@lru_cache(maxsize=4096)
def _char_renderable(font_path: str, size: int, char: str) -> bool:
    """True when the installed font draws a real glyph rather than its .notdef box."""
    font, missing = _font_and_missing_glyph(font_path, size)
    image = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(image).text((0, 0), char, font=font, fill=255)
    data = image.tobytes()
    if data == missing:
        return False
    return bool(data.strip(b"\x00")) or char.isspace()


def primary_keyword(item: Dict[str, Any], font_path: str, size: int = CELL_FONT_SIZE) -> str:
    """Shortest keyword the font can actually draw; ASCII key when none qualify."""
    for name in sorted(item["keywords"], key=len):
        if name and all(_char_renderable(font_path, size, char) for char in name):
            return name
    return item["key"]


def render_index(
    items: List[Dict[str, Any]], total: int, max_pixels: int, max_bytes: int, *, font_path: Optional[str] = None
) -> bytes:
    """Render every primary keyword as one index image; no network, markup or browser.

    font_path is for a trusted local caller (not chat input); normal operation
    selects an installed CJK font. No font download or unreadable silent fallback.
    """
    if not items or len(items) > MAX_ITEMS:
        raise BridgeError("索引内容为空或超过上限。")
    width, margin, row_height, header_height, footer_height = 1120, 24, 34, 112, 46
    cols = 6
    while True:
        cell_width = (width - 2 * margin) / cols
        rows = -(-len(items) // cols)
        height = header_height + rows * row_height + footer_height
        if width * height <= max_pixels or cols >= 10:
            break
        cols += 1
    if width * height > max_pixels:
        raise BridgeError(f"索引图超过像素限制（上限 {format_pixels(max_pixels)}），请提高菜单像素限额。")
    candidate = font_path or next((p for p in FONT_PATHS if Path(p).is_file()), "")
    if not candidate:
        raise BridgeError("索引图缺少本地中日韩字体，请安装 Noto Sans CJK 或 Unifont。")
    try:
        title = ImageFont.truetype(candidate, 30)
        small = ImageFont.truetype(candidate, 21)
        cell_font, _ = _font_and_missing_glyph(candidate, CELL_FONT_SIZE)
        footer_font = ImageFont.truetype(candidate, 18)
    except OSError:
        raise BridgeError("索引图无法读取本地字体。") from None
    keywords = [primary_keyword(x, candidate) for x in items]
    image = Image.new("RGB", (width, height), "#f4f6fa")
    draw = ImageDraw.Draw(image)

    def cell(name: str, x: float, y: int, cell_width: float) -> None:
        # Bound before measuring glyphs, strip control chars, never interpret markup.
        text = "".join(c for c in name[:64] if c.isprintable())
        if draw.textlength(text, font=cell_font) > cell_width:
            count = 0
            while count < len(text) and draw.textlength(text[: count + 1] + "…", font=cell_font) <= cell_width:
                count += 1
            text = text[:count] + "…"
        draw.text((x, y), text, font=cell_font, fill="#18253c")

    draw.text((margin, 14), "表情关键词索引", font=title, fill="#18253c")
    draw.text(
        (margin, 58),
        f"共 {total} 个 · 关键词 + @目标/附图 即可发送 · 详情：/表情帮助 关键词",
        font=small,
        fill="#34577f",
    )
    draw.line((margin, 96, width - margin, 96), fill="#dfe8f6", width=2)
    for index, name in enumerate(keywords):
        row, col = divmod(index, cols)
        cell(name, margin + col * cell_width, header_height + row * row_height + 7, cell_width - 8)
    footer = header_height + rows * row_height
    draw.line((margin, footer - 4, width - margin, footer - 4), fill="#dfe8f6", width=2)
    draw.text(
        (margin, footer + 14),
        "长词可能省略；完整参数：/表情详情 关键词",
        font=footer_font,
        fill="#34577f",
    )
    output = BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    if len(data) > max_bytes:
        raise BridgeError(f"索引图超过输出字节限制（上限 {format_bytes(max_bytes)}）。")
    return data
