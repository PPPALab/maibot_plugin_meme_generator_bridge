from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

import asyncio
import base64
import copy
import json
import logging

import httpx
import pytest

from maibot_plugin_meme_generator_bridge.client import BridgeError, MemeClient, image_kind
from maibot_plugin_meme_generator_bridge.config import BridgeConfig, Settings
from maibot_plugin_meme_generator_bridge.inputs import collect_images, parse_command
from maibot_plugin_meme_generator_bridge.plugin import create_plugin


PNG = b"\x89PNG\r\n\x1a\n" + b"mock-image"
GIF = b"GIF89a" + b"mock-image"
ID = "a" * 32
ITEM = {
    "key": "test",
    "keywords": ["测试"],
    "params": {
        "min_images": 0,
        "max_images": 2,
        "min_texts": 1,
        "max_texts": 2,
        "default_texts": ["默认"],
        "options": [],
    },
}


def settings(**values):
    return Settings(base_url="http://engine:2233", **values)


def ctx():
    return SimpleNamespace(
        send=SimpleNamespace(
            text=AsyncMock(return_value=True), image=AsyncMock(return_value=True), emoji=AsyncMock(return_value=True)
        ),
        message=SimpleNamespace(get_by_id=AsyncMock()),
        chat=SimpleNamespace(
            get_avatar=AsyncMock(),
            open_session=AsyncMock(side_effect=lambda **route: {"success": True, "session_id": "stream", **route}),
        ),
        logger=logging.getLogger("test"),
    )


async def plugin(**values):
    p = create_plugin()
    p.set_plugin_config(
        {
            "plugin": {
                "config_version": "1.0.0",
                "enabled": True,
                "base_url": "http://engine:2233",
                "allowed_groups": ["12345"],
                "cooldown": 0,
                **values,
            },
            "keywords": {"enabled": False},
            "menu": {"image_enabled": False},
        }
    )
    p._set_context(ctx())
    await p.on_load()
    await p.client.close()
    p.client = MemeClient(p.config.plugin, httpx.MockTransport(lambda request: httpx.Response(200, json=[ITEM])))
    p.client.gate = p._heavy_gate
    return p


def message(text="/做表情 测试 hello", media=None):
    return {
        "message_id": "msg",
        "session_id": "stream",
        "raw_message": [{"type": "text", "data": text}, *(media or [])],
    }


def args(text="/表情列表", **values):
    return dict(
        text=text, message=message(text), stream_id="stream", group_id="12345", user_id="23456", platform="qq", **values
    )


async def test_metadata_cache_resolve_and_ambiguity():
    requests = []

    def handler(request):
        requests.append(request)
        other = copy.deepcopy(ITEM)
        other["key"] = "other"
        return httpx.Response(200, json=[ITEM, other])

    client = MemeClient(settings(), httpx.MockTransport(handler))
    assert (await client.resolve("test"))["key"] == "test"
    with pytest.raises(BridgeError, match="重名.*test.*other"):
        await client.resolve("测试")
    with pytest.raises(BridgeError, match="未找到"):
        await client.resolve("missing")
    assert len(requests) == 1
    client._expires = 0
    await client.metadata()
    assert len(requests) == 2
    await client.close()


@pytest.mark.parametrize("disabled", [["test"], ["测试"]])
async def test_disabled(disabled):
    client = MemeClient(
        settings(disabled_templates=disabled), httpx.MockTransport(lambda r: httpx.Response(200, json=[ITEM]))
    )
    with pytest.raises(BridgeError, match="禁用"):
        await client.resolve("test")
    await client.close()


@pytest.mark.parametrize("images", [[], [PNG, GIF]])
async def test_generate_contract(images):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/image/" + ID:
            return httpx.Response(200, content=GIF)
        return httpx.Response(200, json={"image_id": ID})

    client = MemeClient(settings(), httpx.MockTransport(handler))
    result, kind = await client.generate(ITEM, ["hello", "世界"], images)
    assert result == GIF and kind == "gif"
    assert len(calls) == len(images) + 2
    for request, original in zip(calls, images):
        assert request.url.path == "/image/upload"
        payload = json.loads(request.content)
        assert payload == {"type": "data", "data": base64.b64encode(original).decode()}
    payload = json.loads(calls[-2].content)
    assert payload == {
        "images": [{"name": f"image{i + 1}", "id": ID} for i in range(len(images))],
        "texts": ["hello", "世界"],
        "options": {},
    }
    assert calls[-2].url.path == "/memes/test"
    await client.close()


async def test_preview_contract():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return (
            httpx.Response(200, content=PNG)
            if request.url.path.startswith("/image/")
            else httpx.Response(200, json={"image_id": ID})
        )

    client = MemeClient(settings(), httpx.MockTransport(handler))
    assert await client.preview("test") == (PNG, "png")
    assert paths == ["/memes/test/preview", "/image/" + ID]
    await client.close()


@pytest.mark.parametrize(
    "texts,images,changes,match",
    [
        ([], [], {}, "文字数量"),
        (["x"], [PNG] * 3, {}, "图片数量"),
        (["long"], [], {"max_text_chars": 1}, "文字超过"),
        (["x"], [PNG * 100], {"max_input_bytes": 1024}, "大小"),
    ],
)
async def test_invalid_inputs_before_http(texts, images, changes, match):
    def handler(request):
        pytest.fail("invalid input must not reach HTTP")

    client = MemeClient(settings(**changes), httpx.MockTransport(handler))
    with pytest.raises(BridgeError, match=match):
        await client.generate(ITEM, texts, images)
    await client.close()


@pytest.mark.parametrize(
    "mode,match",
    [
        ("offline", "无法连接"),
        ("timeout", "超时"),
        ("business", "文字过长"),
        ("404", "HTTP 404"),
        ("redirect", "HTTP 302"),
        ("large", "大小"),
        ("chunked", "大小"),
        ("badjson", "JSON"),
        ("compressed", "压缩"),
    ],
)
async def test_http_errors_bounded_private_no_retry(mode, match):
    count = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(4):
                yield b"x" * 1024

    def handler(request):
        nonlocal count
        count += 1
        if mode == "offline":
            raise httpx.ConnectError("secret-input", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("secret-input", request=request)
        if mode == "business":
            return httpx.Response(500, json={"code": 560, "message": "secret-input", "data": {"text": "secret-input"}})
        if mode == "404":
            return httpx.Response(404, text="secret-input")
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        if mode == "large":
            return httpx.Response(200, content=b"x" * 2048)
        if mode == "chunked":
            return httpx.Response(200, stream=Stream())
        if mode == "compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Stream())
        return httpx.Response(200, content=b"invalid json")

    client = MemeClient(settings(max_metadata_bytes=1024), httpx.MockTransport(handler))
    with pytest.raises(BridgeError, match=match) as error:
        await client.json_request("GET", "/meme/infos")
    assert "secret-input" not in str(error.value)
    assert count == 1
    await client.close()


@pytest.mark.parametrize("data", [{}, [{"key": "bad"}], [dict(ITEM, key="../bad")], [ITEM, ITEM]])
async def test_invalid_metadata(data):
    client = MemeClient(settings(), httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    with pytest.raises(BridgeError, match="元数据"):
        await client.metadata()
    await client.close()


async def test_current_images_order():
    context = ctx()
    client = MemeClient(settings(), httpx.MockTransport(lambda r: httpx.Response(200, content=GIF)))
    m = message(
        media=[
            {"type": "image", "data": "caption", "hash": "h"},
            {"type": "emoji", "data": "", "hash": "g", "binary_data_base64": base64.b64encode(GIF).decode()},
        ]
    )
    m["raw_message"][1]["binary_data_base64"] = base64.b64encode(PNG).decode()
    assert await collect_images(context, client, m, "stream") == [PNG, GIF]
    context.message.get_by_id.assert_not_awaited()
    context.chat.get_avatar.assert_not_awaited()
    await client.close()


@pytest.mark.parametrize(
    "media,match",
    [
        ([{"type": "reply", "data": {}}], "引用图"),
        ([{"type": "forward", "data": []}], "转发图"),
        ([{"type": "at", "data": {"target_user_id": "nickname"}}], "真实 QQ"),
        ([{"type": "image", "data": "http://127.0.0.1", "binary_data_base64": "!!!!"}], "Base64"),
    ],
)
async def test_reject_unsafe_media(media, match):
    client = MemeClient(settings())
    with pytest.raises(BridgeError, match=match):
        await collect_images(ctx(), client, message(media=media), "stream")
    await client.close()


async def test_missing_binaries_and_avatar_unsupported():
    client = MemeClient(settings())
    context = ctx()
    with pytest.raises(BridgeError, match="无可用 SDK"):
        await collect_images(context, client, message(media=[{"type": "image"}]), "stream")
    context.chat.get_avatar.return_value = {"status": "unsupported"}
    with pytest.raises(BridgeError, match="不支持头像"):
        await collect_images(
            context, client, message(media=[{"type": "at", "data": {"target_user_id": "12345"}}]), "stream"
        )
    await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://q1.qlogo.cn/g?b=qq&nk=12345",
        "https://127.0.0.1/g?nk=12345",
        "https://q1.qlogo.cn.evil/g?b=qq&nk=12345",
        "https://q1.qlogo.cn/g?b=qq&nk=99999",
        "https://q1.qlogo.cn/g?b=qq&nk=12345&url=http://localhost",
        "https://user@q1.qlogo.cn/g?b=qq&nk=12345",
    ],
)
async def test_sdk_avatar_url_never_downloaded(url):
    client = MemeClient(settings(), httpx.MockTransport(lambda r: pytest.fail("unsafe download")))
    context = ctx()
    context.chat.get_avatar.return_value = {"status": "available", "url": url}
    m = message(media=[{"type": "at", "data": {"target_user_id": "12345", "target_user_nickname": "not-an-id"}}])
    m.update(account_id="90000", scope=None)
    with pytest.raises(BridgeError, match="仅提供头像 URL"):
        await collect_images(context, client, m, "stream")
    context.chat.get_avatar.assert_awaited_once_with(
        platform="qq", target_id="12345", target_type="user", account_id="90000", scope=""
    )
    assert not hasattr(client, "avatar")
    await client.close()


@pytest.mark.parametrize("text", ['/做表情 测试 "bad', "x" * 8001])
def test_parse_invalid(text):
    with pytest.raises(BridgeError):
        parse_command(text, settings())


def test_parse_quotes_and_signatures():
    assert parse_command('/做表情 测试 "hello world" "第二行"', settings())[1] == ["测试", "hello world", "第二行"]
    assert image_kind(PNG) == "png"
    assert image_kind(GIF) == "gif"
    with pytest.raises(BridgeError):
        image_kind(b"not image")


@pytest.mark.parametrize("url", ["file:///data", "http://user:pass@engine", "http://engine/path", "http://engine?x=1"])
def test_config_url(url):
    with pytest.raises(ValidationError):
        Settings(base_url=url)


async def test_sdk_commands_and_send_routes():
    p = await plugin()
    await p.client.close()

    def handler(request):
        if request.url.path == "/meme/infos":
            return httpx.Response(200, json=[ITEM])
        if request.url.path.startswith("/image/"):
            return httpx.Response(200, content=GIF)
        return httpx.Response(200, json={"image_id": ID})

    p.client = MemeClient(p.config.plugin, httpx.MockTransport(handler))
    for text in ["/表情帮助", "/表情列表", "/表情详情 测试", "/表情预览 测试", '/做表情 测试 "hello world"']:
        assert (await p.handle_command(**args(text)))[0]
    assert p.ctx.send.text.await_count == 3
    assert p.ctx.send.emoji.await_count == 2
    p.ctx.send.image.assert_not_awaited()
    assert {x["type"] for x in p.get_components()} == {"COMMAND", "HOOK_HANDLER"}
    await p.on_unload()
    assert p.client.http.is_closed


async def test_access_cooldown_queue_and_unload():
    p = await plugin(cooldown=60, concurrency=1, max_waiters=0)
    assert not (await p.handle_command(**{**args(), "group_id": "other"}))[0]
    p.ctx.send.text.assert_not_awaited()
    assert (await p.handle_command(**args("/表情帮助")))[0]
    assert not (await p.handle_command(**args("/表情帮助")))[0]
    assert "冷却" in p.ctx.send.text.await_args.args[0]
    started = asyncio.Event()

    async def slow(*a):
        started.set()
        await asyncio.Event().wait()

    p._execute = slow
    task = asyncio.create_task(p.handle_command(**{**args(), "user_id": "30000"}))
    await started.wait()
    assert not (await p.handle_command(**{**args(), "user_id": "30001"}))[0]
    assert "队列已满" in p.ctx.send.text.await_args.args[0]
    await p.on_unload()
    assert task.cancelled() and p._pending == 0 and not p._tasks


def test_real_sdk_config_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "_manifest.json").read_text())
    assert manifest["manifest_version"] == 2
    assert set(manifest["capabilities"]) == {
        "send.text",
        "send.image",
        "send.emoji",
        "chat.get_avatar",
        "chat.open_session",
    }
    assert manifest["dependencies"] == [
        {"type": "python_package", "name": "httpx", "version_spec": ">=0.27,<0.29"},
        {"type": "python_package", "name": "httpcore", "version_spec": ">=1,<2"},
        {"type": "python_package", "name": "Pillow", "version_spec": ">=10,<13"},
    ]
    assert (root / "requirements.txt").read_text().splitlines() == [
        "httpx>=0.27,<0.29",
        "httpcore>=1,<2",
        "Pillow>=10,<13",
    ]
    assert not BridgeConfig().plugin.enabled
    p = create_plugin()
    p.set_plugin_config(p.get_default_config())
    assert p.config.plugin.allowed_groups == []
    assert p.get_webui_config_schema()


async def test_hook_current_media_bound_consumed_and_scoped():
    p = await plugin()
    m = message(media=[{"type": "image", "hash": "hash", "binary_data_base64": base64.b64encode(PNG).decode()}])
    m.update(platform="qq", message_info={"group_info": {"group_id": "12345"}})
    await p.capture_current_media(message=m, command_name="other")
    assert not p._media
    await p.capture_current_media(message=m, command_name="meme_bridge")
    assert len(p._media) == 1
    stripped = copy.deepcopy(m)
    del stripped["raw_message"][1]["binary_data_base64"]
    assert "binary_data_base64" not in p._attach_current_media(stripped, "other")["raw_message"][1]
    attached = p._attach_current_media(stripped, "stream")
    assert await collect_images(p.ctx, p.client, attached, "stream") == [PNG]
    assert not p._media
    assert "binary_data_base64" not in stripped["raw_message"][1]
    await p.capture_current_media(message=m, command_name="meme_bridge")
    stripped["raw_message"][1]["hash"] = "changed"
    with pytest.raises(BridgeError, match="结构不一致"):
        p._attach_current_media(stripped, "stream")
    await p.on_unload()


async def test_queue_timeout_operation_timeout_and_config_reload():
    p = await plugin(concurrency=1, max_waiters=1, queue_timeout=0.1, operation_timeout=1)
    await p._slots.acquire()
    assert not (await p.handle_command(**args("/表情帮助")))[0]
    assert "等待服务超时" in p.ctx.send.text.await_args.args[0]
    p._slots.release()

    async def slow(*a):
        await asyncio.Event().wait()

    p._execute = slow
    assert not (await p.handle_command(**args("/表情帮助")))[0]
    assert "总耗时超限" in p.ctx.send.text.await_args.args[0]
    old_client = p.client
    await p.on_config_update("self", {}, "1.0.0")
    assert old_client.http.is_closed and p.client is not old_client
    assert p._pending == 0
    await p.on_unload()


async def test_png_send_and_send_failure():
    p = await plugin()
    p.client.resolve = AsyncMock(return_value=ITEM)
    p.client.preview = AsyncMock(return_value=(PNG, "png"))
    assert (await p.handle_command(**args("/表情预览 test")))[0]
    p.ctx.send.image.assert_awaited_once()
    p.ctx.send.image.return_value = False
    assert not (await p.handle_command(**args("/表情预览 test")))[0]
    assert "发送失败" in p.ctx.send.text.await_args.args[0]
    await p.on_unload()
