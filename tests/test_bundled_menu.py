"""Shipped artwork, live-template compatibility and render-free request behavior."""

from pathlib import Path
from unittest.mock import AsyncMock

import asyncio
import base64
import json
import subprocess
import sys

import pytest

from test_bridge import args, plugin

from maibot_plugin_meme_generator_bridge import menu


@pytest.fixture
def fresh_bundle_cache():
    menu.load_bundled_index.cache_clear()
    yield
    menu.load_bundled_index.cache_clear()


async def test_repeated_requests_reuse_bytes_without_disk_or_render_workers(monkeypatch, bundled_items):
    p = await plugin()
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(return_value=bundled_items)
    bundle = p._menu

    def forbidden(*a, **kw):
        raise AssertionError("Menu requests must reuse loaded artwork")

    monkeypatch.setattr(menu, "_read_asset", forbidden)
    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    try:
        for command in ["/表情帮助", "/表情列表", "/表情帮助"]:
            assert await p.handle_command(**args(command)) == (True, None, 1)
        assert p._menu is bundle
        assert p.ctx.send.image.await_count == 3
        assert all(base64.b64decode(call.args[0]) == bundle.data for call in p.ctx.send.image.await_args_list)
        p.ctx.send.text.assert_not_awaited()
    finally:
        await p.on_unload()


async def test_cancelled_menu_send_leaves_no_render_job_and_next_request_works(monkeypatch, bundled_items):
    p = await plugin(concurrency=1, max_waiters=0)
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(return_value=bundled_items)
    entered = asyncio.Event()

    async def send(*a):
        entered.set()
        await asyncio.Future()

    def forbidden(*a, **kw):
        raise AssertionError("A cancelled menu must not leave filesystem/render workers")

    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    p.ctx.send.image.side_effect = send
    task = asyncio.create_task(p.handle_command(**args("/表情帮助")))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert p._pending == 0 and not p._tasks
        p.ctx.send.image.side_effect = None
        assert await p.handle_command(**args("/表情帮助")) == (True, None, 1)
        p.ctx.send.text.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await p.on_unload()


async def test_metadata_order_and_unshown_fields_do_not_invalidate_artwork(bundled_items):
    p = await plugin()
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(
        return_value=[
            {**item, "keywords": item["keywords"][::-1], "date_modified": "changed"} for item in bundled_items[::-1]
        ]
    )
    try:
        assert await p.handle_command(**args("/表情列表")) == (True, None, 1)
        p.ctx.send.image.assert_awaited_once()
        p.ctx.send.text.assert_not_awaited()
    finally:
        await p.on_unload()


@pytest.mark.parametrize("change", ["added", "removed", "renamed", "keyword", "disabled_key", "disabled_keyword"])
@pytest.mark.parametrize("fallback", [True, False])
async def test_changed_or_disabled_templates_never_send_inaccurate_artwork(change, fallback, bundled_items):
    p = await plugin()
    p.config.menu.image_enabled = True
    p.config.menu.text_fallback = fallback
    if change == "added":
        bundled_items.append({"key": "custom_template", "keywords": ["自定义模板"]})
    elif change == "removed":
        bundled_items.pop()
    elif change == "renamed":
        bundled_items[0]["key"] = "renamed_template"
    elif change == "keyword":
        bundled_items[0]["keywords"] = ["新关键词"]
    else:
        p.config.plugin.disabled_templates = [
            bundled_items[0]["key"] if change == "disabled_key" else bundled_items[0]["keywords"][0]
        ]
    p.client.metadata = AsyncMock(return_value=bundled_items)
    try:
        assert await p.handle_command(**args("/表情列表")) == (fallback, None, 1)
        p.ctx.send.image.assert_not_awaited()
        p.ctx.send.text.assert_awaited_once()
        text = p.ctx.send.text.await_args.args[0]
        assert "随包菜单不同" in text
        assert ("文字清单" in text) is fallback
        if fallback:
            visible = sum(not p.client.disabled(item) for item in bundled_items)
            assert f"共 {visible} 个" in text
    finally:
        await p.on_unload()


@pytest.mark.parametrize("limit", ["pixels", "bytes"])
@pytest.mark.parametrize("fallback", [True, False])
async def test_bundled_artwork_obeys_configured_limits(limit, fallback, bundled_items):
    p = await plugin()
    p.config.menu.image_enabled = True
    p.config.menu.text_fallback = fallback
    p.client.metadata = AsyncMock(return_value=bundled_items)
    if limit == "pixels":
        p.config.menu.max_pixels = p._menu.width * p._menu.height - 1
    else:
        p.config.plugin.max_output_bytes = len(p._menu.data) - 1
    try:
        assert await p.handle_command(**args("/表情帮助")) == (fallback, None, 1)
        p.ctx.send.image.assert_not_awaited()
        text = p.ctx.send.text.await_args.args[0]
        assert ("像素限制" if limit == "pixels" else "输出字节限制") in text
        assert ("文字清单" in text) is fallback
    finally:
        await p.on_unload()


@pytest.mark.parametrize("damage", ["missing", "image", "manifest", "oversized", "dimensions"])
@pytest.mark.parametrize("fallback", [True, False])
async def test_missing_or_damaged_bundle_loads_plugin_and_respects_fallback(
    tmp_path, monkeypatch, fresh_bundle_cache, bundled_items, damage, fallback
):
    for path in menu.BUNDLE_DIR.iterdir():
        if path.suffix in {".json", ".png"}:
            (tmp_path / path.name).write_bytes(path.read_bytes())
    png = tmp_path / "menu-v0.2.3.png"
    manifest = tmp_path / "menu-v0.2.3.json"
    if damage == "missing":
        png.unlink()
    elif damage == "image":
        data = png.read_bytes()
        png.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    elif damage == "manifest":
        manifest.write_text("{", encoding="utf-8")
    elif damage == "oversized":
        png.write_bytes(b"x" * 1_000_001)
    else:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["height"] += 1
        manifest.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(menu, "BUNDLE_DIR", tmp_path)
    p = await plugin()
    p.config.menu.image_enabled = True
    p.config.menu.text_fallback = fallback
    p.client.metadata = AsyncMock(return_value=bundled_items)
    try:
        assert p._menu is None
        assert await p.handle_command(**args("/表情帮助")) == (fallback, None, 1)
        p.ctx.send.image.assert_not_awaited()
        text = p.ctx.send.text.await_args.args[0]
        assert "随包菜单文件缺失或损坏" in text and str(tmp_path) not in text
        assert ("文字清单" in text) is fallback
    finally:
        await p.on_unload()


def test_runtime_loads_without_pillow_or_font_access():
    code = """
import sys
class NoPillow:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PIL' or fullname.startswith('PIL.'):
            raise AssertionError('Runtime attempted to load Pillow')
sys.meta_path.insert(0, NoPillow())
from maibot_plugin_meme_generator_bridge.plugin import create_plugin
from maibot_plugin_meme_generator_bridge.menu import load_bundled_index
bundle = load_bundled_index()
assert len(bundle.templates) == 300 and bundle.data.startswith(b'\\x89PNG')
assert 'maibot_plugin_meme_generator_bridge.tools.build_menu' not in sys.modules
assert create_plugin() is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
