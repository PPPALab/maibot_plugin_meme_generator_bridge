from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import base64
import copy
import json
import socket
import struct
import subprocess

import httpcore
import httpx
import pytest

from host_dispatch import Dispatch
from test_bridge import GIF, ITEM, PNG, args, ctx, message, plugin, settings

from maibot_plugin_meme_generator_bridge.client import BridgeError, MemeClient
from maibot_plugin_meme_generator_bridge.config import AvatarSettings
from maibot_plugin_meme_generator_bridge.inputs import collect_images, command_text
from maibot_plugin_meme_generator_bridge.keywords import KeywordIndex
from maibot_plugin_meme_generator_bridge.media import AvatarDownloader, PublicBackend, avatar_url


MENU = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 400, 400)
HAMMER = {
    "key": "hammer",
    "keywords": ["锤"],
    "params": {
        "min_images": 1,
        "max_images": 1,
        "min_texts": 0,
        "max_texts": 0,
    },
}


def media_message(text="锤", media=None):
    return {
        **message(text, media),
        "platform": "qq",
        "account_id": "90000",
        "scope": None,
        "message_info": {"user_info": {"user_id": "23456"}, "group_info": {"group_id": "12345"}},
    }


def item(key, name):
    return {**copy.deepcopy(ITEM), "key": key, "keywords": [name]}


async def finish_jobs(p):
    await asyncio.gather(*list(p._keyword_jobs))
    if p._busy_notice:
        await p._busy_notice


@pytest.mark.parametrize(
    "text,media,expected",
    [
        ("锤", [], "锤"),
        ("锤", [{"type": "at", "data": {"target_user_id": "12345"}}], "锤"),
        ("锤", [{"type": "image"}], "锤"),
        ('锤 "你好 世界"', [], "锤"),
        ("锤子 你好", [], "锤子"),
        ("锤子坏了", [], None),
        ("锤坏了", [], None),
        ("今天锤", [], None),
        (" 锤", [], "锤"),
        ("锤@某人", [], None),
        ("/做表情 锤", [], None),
        ("其它消息", [], None),
        ("锤", [{"type": "text", "data": "子坏了"}], None),
        ("锤", [{"type": "text", "data": " "}, {"type": "at", "data": {"target_user_id": "12345"}}], "锤"),
        ("锤", [{"type": "text", "data": "子"}, {"type": "image"}], "锤子"),
    ],
)
def test_longest_keyword_and_strict_boundaries(text, media, expected):
    index = KeywordIndex([HAMMER, item("long", "锤子")], [])
    result = index.match(message(text, media))
    assert (result[0] if result else None) == expected
    if result:
        assert command_text(result[1], "").startswith("/做表情 ")


def test_keyword_exclusions_and_untrusted_regex():
    index = KeywordIndex([HAMMER, item("special", "a.*")], ["锤"])
    assert index.match(message("锤")) is None
    assert index.match(message("abc")) is None
    assert index.match(message("a.*"))[0] == "a.*"


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "missing_image",
        "ambiguous",
        "disabled_template",
        "cooldown",
        "queue_full",
        "timeout",
        "offline",
        "exception",
        "bad_quotes",
        "unsupported_avatar",
    ],
)
async def test_keyword_every_outcome_aborts_host_before_media_and_llm(case):
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    if case in {"ambiguous", "disabled_template", "offline"}:
        p.client.resolve.side_effect = BridgeError(
            {
                "ambiguous": "模板名重名，请使用精确 key：hammer、other",
                "disabled_template": "该模板已禁用。",
                "offline": "服务离线",
            }[case]
        )
    if case == "timeout":
        p.client.generate.side_effect = BridgeError("服务请求超时")
    if case == "exception":
        p.client.generate.side_effect = RuntimeError("private detail")
    if case == "cooldown":
        p.client.settings.cooldown = 60
        p._cooldowns[("qq", "23456")] = asyncio.get_running_loop().time()
    if case == "queue_full":
        p._pending = p.client.settings.concurrency + p.client.settings.max_waiters
    dispatch = Dispatch(p)
    raw = [{"type": "image", "hash": "h", "binary_data_base64": base64.b64encode(PNG).decode()}]
    if case == "missing_image":
        raw = []
        p.client.generate.side_effect = BridgeError("图片数量要求 1–1，实际 0。")
    if case == "unsupported_avatar":
        raw = [{"type": "at", "data": {"target_user_id": "12345"}}]
        p.ctx.chat.get_avatar.return_value = {"status": "unsupported"}
    m = dispatch.message('锤 "bad' if case == "bad_quotes" else "锤", media=raw)
    if case == "group_denied":
        m.message_info.group_info.group_id = "other"
    if case == "private_denied":
        m.message_info.group_info = None
    try:
        await dispatch.bot.receive_message(m)
        m.process.assert_not_awaited()
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        assert not dispatch.invocations  # no broad/static keyword Command registration
        await finish_jobs(p)
        dispatch.bot.heartflow_message_receiver.process_message.assert_not_awaited()
        if case == "success":
            p.ctx.send.emoji.assert_awaited_once()
            p.client.resolve.assert_awaited_once_with("锤")
            assert p.client.generate.await_args.args[1:] == ([], [PNG])
    finally:
        await p.on_unload()


async def test_consume_is_immediate_before_background_generation_wait():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(*a):
        started.set()
        await release.wait()

    p._execute = slow
    try:
        assert await p.handle_keyword(media_message()) == {"action": "abort"}
        await asyncio.wait_for(started.wait(), 1)
        assert p._keyword_jobs and all(not task.done() for task in p._keyword_jobs)
        release.set()
        await finish_jobs(p)
    finally:
        release.set()
        await p.on_unload()


async def test_refresh_swaps_snapshot_once_and_keeps_old_on_failure():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    before = p._index
    p.client.metadata = AsyncMock(return_value=[item("new", "新模板")])
    await p._refresh_keywords()
    assert p._index is not before
    assert p._index.match(message("锤")) is None
    assert p._index.match(message("新模板")) is not None
    stable = p._index
    p.client.metadata.side_effect = BridgeError("offline")
    await p._refresh_keywords()
    assert p._index is stable
    updated = p.get_plugin_config_data()
    updated["keywords"] = {"enabled": False, "exclusions": ["新模板"], "refresh_seconds": 10}
    old_http, old_avatar = p.client, p.avatars
    await p.on_config_update("self", updated, "1.1.0")
    assert old_http.http.is_closed and not p._index.names and not p._keyword_jobs
    assert p.avatars is not old_avatar
    assert await p.handle_keyword(media_message("新模板")) is None
    await p.on_unload()


async def test_keyword_conflict_exclusions_and_unknown_are_passed_to_host():
    p = await plugin()
    p.config.keywords.enabled = True
    p.config.keywords.exclusions = ["锤"]
    p._set_index([HAMMER])
    dispatch = Dispatch(p)
    try:
        for text in ["锤", "锤子坏了", "普通消息"]:
            m = dispatch.message(text)
            await dispatch.bot.receive_message(m)
            m.process.assert_awaited_once()
        assert dispatch.bot.heartflow_message_receiver.process_message.await_count == 3
        assert not p._keyword_jobs
    finally:
        await p.on_unload()


async def test_keyword_admission_is_bounded_and_unload_cancels_waiting_jobs():
    p = await plugin(concurrency=1, max_waiters=0)
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    never = asyncio.Event()
    p._execute = lambda *a: never.wait()
    assert await p.handle_keyword(media_message()) == {"action": "abort"}
    assert await p.handle_keyword(media_message()) == {"action": "abort"}
    assert len(p._keyword_jobs) == 1
    await asyncio.sleep(0)
    await p.on_unload()
    assert not p._keyword_jobs and not p._tasks and p._keyword_bytes == 0


async def test_menu_index_excludes_disabled_and_counts_remaining(monkeypatch):
    calls = []
    items = [item("a", "第一项"), item("b", "第二项"), item("hide", "禁用项")]
    from maibot_plugin_meme_generator_bridge import plugin as plugin_module

    def draw(selected, total, pixels, max_bytes):
        calls.append(([x["key"] for x in selected], total))
        return MENU

    monkeypatch.setattr(plugin_module, "render_index", draw)
    p = await plugin(disabled_templates=["hide"])
    p.config.menu.image_enabled = True
    p.client.metadata = AsyncMock(return_value=items)
    try:
        await p.handle_command(**args("/表情帮助"))
        assert calls == [(["a", "b"], 2)]
        p.ctx.send.text.assert_not_awaited()
        p.ctx.send.image.assert_awaited_once()
        assert p._index.match(message("第一项")) is not None
    finally:
        await p.on_unload()


@pytest.mark.parametrize("fallback", [True, False])
async def test_menu_failure_visible_not_silent(fallback, monkeypatch):
    p = await plugin()
    p.config.menu.image_enabled = True
    p.config.menu.text_fallback = fallback
    from maibot_plugin_meme_generator_bridge import plugin as plugin_module

    def fail(*a):
        raise BridgeError("菜单绘制失败")

    monkeypatch.setattr(plugin_module, "render_index", fail)
    try:
        result = await p.handle_command(**args("/表情列表"))
        assert result[2] == 1
        text = p.ctx.send.text.await_args.args[0]
        assert "菜单绘制失败" in text
        if fallback:
            assert "测试" in text and "文字清单" in text
    finally:
        await p.on_unload()


async def test_menu_empty_and_send_exception_answers_text_safely():
    p = await plugin(disabled_templates=["test"])
    p.config.menu.image_enabled = True
    await p.handle_command(**args("/表情列表"))
    p.ctx.send.image.assert_not_awaited()
    assert "共 0 个" in p.ctx.send.text.await_args.args[0]
    p.ctx.send.text.reset_mock()
    p.client.metadata = AsyncMock(return_value=[item("a", "第一项")])
    p.ctx.send.image.side_effect = RuntimeError("private endpoint details")
    await p.handle_command(**args("/表情列表"))
    assert p.ctx.send.text.await_count == 1
    reply = p.ctx.send.text.await_args.args[0]
    assert "读取或发送失败" in reply and "private" not in reply and "第一项" in reply
    await p.on_unload()


def test_configuration_schema_chinese_groups_and_roundtrip():
    from maibot_plugin_meme_generator_bridge.plugin import create_plugin

    p = create_plugin()
    schema = p.get_webui_config_schema()["sections"]
    assert set(schema) == {"plugin", "menu", "keywords", "avatar"}
    for section in schema.values():
        assert any("\u4e00" <= char <= "\u9fff" for char in section["title"])
        for field in section["fields"].values():
            assert not field["hidden"] and not field["disabled"] and field["label"]
    data = p.get_default_config()
    data["plugin"].update(base_url="http://engine:2233", enabled=True, allowed_groups=["12345"])
    data["keywords"].update(enabled=False, exclusions=["锤"])
    data["menu"]["max_pixels"] = 7_000_000
    p.set_plugin_config(data)
    assert p.config.plugin.base_url == "http://engine:2233" and p.config.menu.max_pixels == 7_000_000
    assert p.config.keywords.exclusions == ["锤"] and not p.config.keywords.enabled


@pytest.mark.parametrize(
    "url",
    [
        "http://q1.qlogo.cn/g?b=qq&nk=12345&s=640",
        "https://127.0.0.1/g?b=qq&nk=12345&s=640",
        "https://q1.qlogo.cn.evil/g?b=qq&nk=12345&s=640",
        "https://user@q1.qlogo.cn/g?b=qq&nk=12345&s=640",
        "https://q1.qlogo.cn:443/g?b=qq&nk=12345&s=640",
        "https://q1.qlogo.cn/g?b=qq&nk=99999&s=640",
        "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640&url=http://localhost",
        "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640#fragment",
        "https://q1.qlogo.cn/g?b=qq&nk=12345&nk=12345&s=640",
        "file:///etc/passwd",
    ],
)
def test_avatar_url_rejects_arbitrary_inputs(url):
    with pytest.raises(BridgeError):
        avatar_url(url, "12345")


class FakeResponse:
    def __init__(self, chunks, status=200, headers=None):
        self.status, self.chunks, self.headers = status, chunks, headers or []

    async def aiter_stream(self):
        for chunk in self.chunks:
            yield chunk


class FakePool:
    def __init__(self, response):
        self.response, self.calls, self.closed = response, 0, False

    @asynccontextmanager
    async def stream(self, *a, **kw):
        self.calls += 1
        yield self.response

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "response,match",
    [
        (FakeResponse([PNG], status=302), "重定向"),
        (FakeResponse([PNG], headers=[(b"content-length", b"9999999")]), "大小"),
        (FakeResponse([b"a" * 800, b"b" * 800]), "大小"),
        (FakeResponse([PNG], headers=[(b"content-encoding", b"gzip")]), "压缩"),
        (FakeResponse([b"not an image"]), "图片"),
    ],
)
async def test_avatar_response_bounded(response, match):
    pool = FakePool(response)
    downloader = AvatarDownloader(AvatarSettings(), 1024, pool)
    try:
        with pytest.raises(BridgeError, match=match):
            await downloader.get("https://q1.qlogo.cn/g?b=qq&nk=12345&s=640", "12345")
        assert pool.calls == 1 and not downloader._cache
    finally:
        await downloader.close()


async def test_avatar_cache_and_original_media_order_without_duplicates():
    pool = FakePool(FakeResponse([PNG]))
    downloader = AvatarDownloader(AvatarSettings(cache_entries=1), 1024, pool)
    url = "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"
    assert await downloader.get(url, "12345") == PNG
    assert await downloader.get(url, "12345") == PNG and pool.calls == 1
    await downloader.get(url.replace("12345", "23456"), "23456")
    assert pool.calls == 2 and len(downloader._cache) == 1
    await downloader.close()
    assert pool.closed and not downloader._cache
    context = ctx()
    context.chat.get_avatar.side_effect = lambda **kw: {"status": "available", "url": kw["target_id"]}
    avatars = SimpleNamespace(
        settings=AvatarSettings(), get=AsyncMock(side_effect=[PNG + b"avatar1", GIF + b"avatar2"])
    )

    def at(target):
        return {"type": "at", "data": {"target_user_id": target, "target_user_nickname": "ignored"}}

    def img(data):
        return {"type": "image", "binary_data_base64": base64.b64encode(data).decode()}

    m = media_message(media=[at("12345"), img(PNG), at("12345"), at("23456"), img(GIF), img(PNG)])
    client = MemeClient(settings(max_images=8))
    try:
        result = await collect_images(context, client, m, "stream", avatars)
        assert result == [PNG + b"avatar1", PNG, GIF + b"avatar2", GIF]
        assert [call.kwargs["target_id"] for call in context.chat.get_avatar.await_args_list] == ["12345", "23456"]
        assert all(
            call.kwargs["account_id"] == "90000" and call.kwargs["scope"] == ""
            for call in context.chat.get_avatar.await_args_list
        )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "::ffff:127.0.0.1"]
)
async def test_dns_private_local_and_multicast_rejected(monkeypatch, address):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    )
    backend = PublicBackend()
    backend.backend = SimpleNamespace(connect_tcp=AsyncMock())
    with pytest.raises(BridgeError, match="非公网"):
        await backend.connect_tcp("q1.qlogo.cn", 443)
    backend.backend.connect_tcp.assert_not_awaited()


async def test_dns_pinned_tcp_and_original_tls_hostname(monkeypatch):
    loop = asyncio.get_running_loop()
    resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(loop, "getaddrinfo", resolver)

    class Wire(httpcore.AsyncNetworkStream):
        tls_name = None
        sent = False

        async def read(self, max_bytes, timeout=None):
            if self.sent:
                return b""
            self.sent = True
            return (
                b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(PNG)).encode() + b"\r\nConnection: close\r\n\r\n" + PNG
            )

        async def write(self, buffer, timeout=None):
            pass

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            self.tls_name = server_hostname
            assert ssl_context.check_hostname
            return self

        def get_extra_info(self, info):
            return None

    wire = Wire()
    backend = PublicBackend()
    connect = AsyncMock(return_value=wire)
    backend.backend = SimpleNamespace(connect_tcp=connect)
    pool = httpcore.AsyncConnectionPool(network_backend=backend, max_keepalive_connections=0)
    downloader = AvatarDownloader(AvatarSettings(), 1024, pool)
    try:
        assert await downloader.get("https://q1.qlogo.cn/g?b=qq&nk=12345&s=640", "12345") == PNG
        assert connect.await_args.args[:2] == ("8.8.8.8", 443)
        assert wire.tls_name == "q1.qlogo.cn" and resolver.await_count == 1
    finally:
        await downloader.close()


def test_local_notes_ignored_and_not_tracked():
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").is_dir():
        pytest.skip("source archive has no git metadata")
    assert subprocess.run(["git", "check-ignore", "LOCAL_NOTES.md"], cwd=root, capture_output=True).returncode == 0
    tracked = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    assert "LOCAL_NOTES.md" not in tracked


async def test_avatar_timeout_cache_expiry_and_concurrency():
    class SlowPool(FakePool):
        active = 0
        maximum = 0
        delay = 0.02

        @asynccontextmanager
        async def stream(self, *a, **kw):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(self.delay)
                self.calls += 1
                yield self.response
            finally:
                self.active -= 1

    pool = SlowPool(FakeResponse([PNG]))
    config = AvatarSettings(concurrency=1)
    downloader = AvatarDownloader(config, 1024, pool)

    def url(target):
        return f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640"

    try:
        await asyncio.gather(*(downloader.get(url(target), target) for target in ["12345", "23456", "34567"]))
        assert pool.maximum == 1 and pool.calls == 3
        downloader._cache[url("12345")] = (0, PNG)
        await downloader.get(url("12345"), "12345")
        assert pool.calls == 4
        config.timeout = 1
        pool.delay = 2
        with pytest.raises(BridgeError, match="超时"):
            await downloader.get(url("45678"), "45678")
        assert pool.active == 0
    finally:
        await downloader.close()


async def test_sdk_avatar_query_and_download_share_one_deadline():
    context = ctx()

    async def never(**kwargs):
        await asyncio.Event().wait()

    context.chat.get_avatar.side_effect = never
    config = AvatarSettings(timeout=1)
    avatars = SimpleNamespace(settings=config, get=AsyncMock())
    client = MemeClient(settings())
    try:
        with pytest.raises(BridgeError, match="头像查询或下载超时"):
            await collect_images(
                context,
                client,
                media_message(media=[{"type": "at", "data": {"target_user_id": "12345"}}]),
                "stream",
                avatars,
            )
        avatars.get.assert_not_awaited()
    finally:
        await client.close()


async def test_dns_rebinding_mixed_answers_rejected(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
        ),
    )
    backend = PublicBackend()
    backend.backend = SimpleNamespace(connect_tcp=AsyncMock())
    with pytest.raises(BridgeError, match="非公网"):
        await backend.connect_tcp("q1.qlogo.cn", 443)
    backend.backend.connect_tcp.assert_not_awaited()


def test_rust_menu_dto_and_actual_hammer_contract_read_only():
    root = Path(__file__).resolve().parents[2] / "meme-generator-rs"
    if not root.is_dir():
        pytest.skip("optional read-only Rust reference not available")
    source = (root / "meme_generator/src/tools/render_list.rs").read_text()
    dto = source.split("pub struct RenderMemeListParams {", 1)[1].split("}", 1)[0]
    assert all(
        f"pub {field}:" in dto
        for field in [
            "meme_properties",
            "exclude_memes",
            "sort_by",
            "sort_reverse",
            "text_template",
            "add_category_icon",
        ]
    )
    handler = (root / "meme_generator_server/src/tools/mod.rs").read_text()
    assert "render_list(Json(payload): Json<RenderMemeListParams>)" in handler
    assert "handle_image_result(result).await" in handler
    hammer = (root / "meme_generator_memes/src/memes/hammer.rs").read_text()
    assert '"hammer"' in hammer and 'keywords = &["锤"]' in hammer
    assert "min_images = 1" in hammer and "max_images = 1" in hammer


async def test_real_ambiguity_and_disabled_keyword_menu_refresh():
    p = await plugin(disabled_templates=["other"])
    p.config.keywords.enabled = True
    metadata = [HAMMER, {**copy.deepcopy(HAMMER), "key": "other"}]
    await p.client.close()
    p.client = MemeClient(p.config.plugin, httpx.MockTransport(lambda r: httpx.Response(200, json=metadata)))
    try:
        await p._refresh_keywords()
        assert await p.handle_keyword(media_message()) == {"action": "abort"}
        await finish_jobs(p)
        error = p.ctx.send.text.await_args.args[0]
        assert "重名" in error and "other[禁用]" in error
        assert not p.ctx.send.image.await_count and not p.ctx.send.emoji.await_count
    finally:
        await p.on_unload()


async def test_load_and_reload_register_only_one_snapshot_refresh_task(monkeypatch):
    import maibot_plugin_meme_generator_bridge.plugin as module

    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[HAMMER])

    monkeypatch.setattr(module, "MemeClient", lambda config: MemeClient(config, httpx.MockTransport(handler)))
    p = module.create_plugin()
    data = p.get_default_config()
    data["plugin"].update(enabled=True, base_url="http://engine:2233", allowed_groups=["12345"])
    p.set_plugin_config(data)
    p._set_context(ctx())
    await p.on_load()
    first_task = p._refresh_task
    assert p._index.names == ("锤",) and calls == ["/meme/infos"]
    try:
        await p.on_config_update("self", data, "1.1.0")
        assert first_task.cancelled() and p._refresh_task is not first_task
        assert not p._refresh_task.done() and calls == ["/meme/infos", "/meme/infos"]
        components = p.get_components()
        assert len({component["name"] for component in components}) == len(components) == 3
    finally:
        await p.on_unload()
    assert p._refresh_task is None


async def test_keyword_adjacent_real_at_uses_only_sdk_avatar_source():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    url = "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"
    p.ctx.chat.get_avatar.return_value = {"status": "available", "url": url}
    p.avatars.get = AsyncMock(return_value=PNG)
    m = media_message("锤", [{"type": "at", "data": {"target_user_id": "12345", "target_user_nickname": "不用于推断"}}])
    try:
        assert await p.handle_keyword(m) == {"action": "abort"}
        await finish_jobs(p)
        p.ctx.chat.get_avatar.assert_awaited_once_with(
            platform="qq", target_id="12345", target_type="user", account_id="90000", scope=""
        )
        p.avatars.get.assert_awaited_once_with(url, "12345")
        assert p.client.generate.await_args.args[1:] == ([], [PNG])
        p.ctx.send.emoji.assert_awaited_once()
    finally:
        await p.on_unload()


async def test_menu_sdk_send_exception_keeps_text_and_safe_error(monkeypatch):
    p = await plugin()
    p.config.menu.image_enabled = True
    from maibot_plugin_meme_generator_bridge import plugin as plugin_module

    monkeypatch.setattr(plugin_module, "render_index", lambda *a: MENU)
    p.ctx.send.image.side_effect = RuntimeError("private endpoint details")
    try:
        result = await p.handle_command(**args("/表情帮助"))
        assert result[2] == 1
        reply = p.ctx.send.text.await_args.args[0]
        assert "测试" in reply and "读取或发送失败" in reply
        assert "private endpoint details" not in reply
    finally:
        await p.on_unload()


async def test_first_keyword_message_opens_official_session_before_sending():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.client.resolve = AsyncMock(return_value=HAMMER)
    p.client.generate = AsyncMock(return_value=(GIF, "gif"))
    opened = False

    async def open_session(**kwargs):
        nonlocal opened
        opened = True
        return {"success": True, "session_id": "stream", **kwargs}

    async def send(*args):
        return opened  # Like Host send_service, a new route cannot send before creation.

    p.ctx.chat.open_session = AsyncMock(side_effect=open_session)
    p.ctx.send.emoji.side_effect = send
    dispatch = Dispatch(p)
    m = dispatch.message("锤", media=[{"type": "image", "binary_data_base64": base64.b64encode(PNG).decode()}])
    try:
        await dispatch.bot.receive_message(m)
        m.process.assert_not_awaited()
        await finish_jobs(p)
        p.ctx.chat.open_session.assert_awaited_once_with(
            platform="qq", chat_type="group", user_id="23456", group_id="12345", account_id="90000", scope="route"
        )
        p.ctx.send.emoji.assert_awaited_once()
        p.ctx.send.text.assert_not_awaited()
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "result",
    [None, {}, {"session_id": "another-route"}]
    + [
        {
            "success": True,
            "session_id": "stream",
            "platform": "qq",
            "chat_type": "group",
            "group_id": "12345",
            "account_id": "90000",
            "scope": "",
            field: "wrong-route",
        }
        for field in ("platform", "chat_type", "group_id", "account_id", "scope")
    ],
)
async def test_keyword_session_mismatch_is_consumed_without_generation(result):
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    p.ctx.chat.open_session.side_effect = None
    p.ctx.chat.open_session.return_value = result
    p.client.generate = AsyncMock()
    try:
        assert await p.handle_keyword(media_message()) == {"action": "abort"}
        await finish_jobs(p)
        p.client.generate.assert_not_awaited()
        assert "聊天流" in p.ctx.send.text.await_args.args[0]
        assert p.ctx.send.text.await_args.args[1] == "stream"
    finally:
        await p.on_unload()


async def test_private_keyword_session_keeps_sdk_route_and_shares_deadline():
    p = await plugin(allow_private=True, operation_timeout=1)
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    m = media_message()
    m["message_info"]["group_info"] = None

    async def never(**kwargs):
        await asyncio.Event().wait()

    p.ctx.chat.open_session.side_effect = never
    p.client.generate = AsyncMock()
    try:
        assert await p.handle_keyword(m) == {"action": "abort"}
        await finish_jobs(p)
        p.ctx.chat.open_session.assert_awaited_once_with(
            platform="qq", chat_type="private", user_id="23456", group_id="", account_id="90000", scope=""
        )
        assert "总耗时" in p.ctx.send.text.await_args.args[0]
        p.client.generate.assert_not_awaited()
    finally:
        await p.on_unload()


async def test_denied_keyword_and_legacy_command_do_not_open_sessions():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    m = media_message()
    m["message_info"]["group_info"]["group_id"] = "denied"
    try:
        assert await p.handle_keyword(m) is None
        await finish_jobs(p)
        await p.handle_command(**args("/表情列表"))
        p.ctx.chat.open_session.assert_not_awaited()
    finally:
        await p.on_unload()


async def test_overlapping_config_updates_close_all_superseded_clients(monkeypatch):
    import maibot_plugin_meme_generator_bridge.plugin as module

    p = await plugin()
    entered, release = asyncio.Event(), asyncio.Event()
    old_close = p.client.close

    async def slow_close():
        entered.set()
        await release.wait()
        await old_close()

    p.client.close = slow_close
    created = []

    def make_client(config):
        client = MemeClient(config, httpx.MockTransport(lambda r: httpx.Response(200, json=[HAMMER])))
        created.append(client)
        return client

    monkeypatch.setattr(module, "MemeClient", make_client)
    first, second = p.get_plugin_config_data(), p.get_plugin_config_data()
    first["plugin"]["base_url"] = "http://first:2233"
    second["plugin"]["base_url"] = "http://second:2233"
    a = asyncio.create_task(p.on_config_update("self", first, "1.1.0"))
    await entered.wait()
    b = asyncio.create_task(p.on_config_update("self", second, "1.1.0"))
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.gather(a, b)
        assert len(created) == 2
        assert created[0].http.is_closed and not created[1].http.is_closed
        assert p.client is created[1] and p.client.settings.base_url == "http://second:2233"
        assert p.config.plugin.base_url == p.client.settings.base_url
    finally:
        release.set()
        await p.on_unload()


def test_sdk_open_session_and_manifest_contract():
    from maibot_sdk.capabilities.chat import ChatCapability

    import inspect

    signature = inspect.signature(ChatCapability.open_session)
    assert {"platform", "chat_type", "user_id", "group_id", "account_id", "scope"} <= set(signature.parameters)
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "_manifest.json").read_text())
    assert "chat.open_session" in manifest["capabilities"]
    host = root.parent / "reference" / "MaiBot"
    if not host.is_dir():
        pytest.skip("optional read-only Host reference unavailable")
    data = (host / "src/plugin_runtime/capabilities/data.py").read_text()
    assert '"session_id": stream.session_id' in data
    assert "**serialized_stream" in data


async def test_real_sdk_open_session_response_is_not_unwrapped():
    from maibot_sdk.context import PluginContext

    route = {
        "platform": "qq",
        "chat_type": "private",
        "user_id": "23456",
        "group_id": "",
        "account_id": "90000",
        "scope": "route",
    }
    response = {"success": True, "created": True, "stream": {"session_id": "stream"}, "session_id": "stream", **route}
    rpc = AsyncMock(return_value=response)
    context = PluginContext("local.meme-generator-bridge", rpc_call=rpc)
    assert await context.chat.open_session(**route) == response
    rpc.assert_awaited_once_with(
        "cap.call", "local.meme-generator-bridge", {"capability": "chat.open_session", "args": route}
    )


@pytest.mark.parametrize("target", ["", "all", "1234", "1" * 21, "１２３４５"])
def test_avatar_rejects_invalid_real_id(target):
    with pytest.raises(BridgeError):
        avatar_url(f"https://q1.qlogo.cn/g?b=qq&nk={target}&s=640", target)


def test_avatar_url_length_bound():
    with pytest.raises(BridgeError):
        avatar_url("https://q1.qlogo.cn/g?b=qq&nk=12345&s=640" + "x" * 2048, "12345")


async def test_reload_cancels_keyword_session_query_before_generation():
    p = await plugin()
    p.config.keywords.enabled = True
    p._set_index([HAMMER])
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_session(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    p.ctx.chat.open_session.side_effect = wait_session
    p.client.generate = AsyncMock()
    generate = p.client.generate
    try:
        assert await p.handle_keyword(media_message()) == {"action": "abort"}
        await asyncio.wait_for(started.wait(), 1)
        updated = p.get_plugin_config_data()
        updated["keywords"]["enabled"] = False
        await p.on_config_update("self", updated, "1.1.0")
        assert cancelled.is_set() and not p._keyword_jobs and p._keyword_bytes == 0 and p._pending == 0
        generate.assert_not_awaited()
        p.ctx.send.image.assert_not_awaited()
        p.ctx.send.emoji.assert_not_awaited()
        assert await p.handle_keyword(media_message()) is None
    finally:
        await p.on_unload()
