from contextlib import asynccontextmanager

import asyncio
import base64
import copy

import httpx
import pytest

from test_bridge import ID, ITEM, PNG, args, plugin, settings
from test_features import FakePool, FakeResponse

from maibot_plugin_meme_generator_bridge.client import BridgeError, HeavyGate, MemeClient, json_body
from maibot_plugin_meme_generator_bridge.config import AvatarSettings
from maibot_plugin_meme_generator_bridge.media import AvatarDownloader


@pytest.mark.parametrize("failure", ["timeout", "network", "cancel"])
@pytest.mark.parametrize(
    "path,method", [("/memes/test", "POST"), ("/memes/test/preview", "GET"), ("/tools/render_list", "POST")]
)
async def test_unknown_heavy_completion_trips_and_next_request_is_not_sent(failure, path, method):
    calls = []
    entered = asyncio.Event()

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/meme/infos":
            return httpx.Response(200, json=[ITEM])
        entered.set()
        if failure == "timeout":
            raise httpx.ReadTimeout("private")
        if failure == "network":
            raise httpx.ConnectError("private")
        await asyncio.Event().wait()

    client = MemeClient(settings(), httpx.MockTransport(handler))
    try:
        task = asyncio.create_task(client.json_request(method, path))
        await entered.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(BridgeError):
                await task
        assert client.gate.blocked and client.gate.active == 0
        before = len(calls)
        with pytest.raises(BridgeError, match="熔断"):
            await client.preview("test")
        with pytest.raises(BridgeError, match="熔断"):
            await client.generate(ITEM, ["hello"], [PNG])
        assert len(calls) == before  # Not even upload #1 is sent.
        assert await client.metadata() == [ITEM]
    finally:
        await client.close()


@pytest.mark.parametrize("status", [200, 500])
async def test_known_http_completion_does_not_trip(status):
    value = {"image_id": ID} if status == 200 else {"code": 530}
    client = MemeClient(settings(), httpx.MockTransport(lambda r: httpx.Response(status, json=value)))
    try:
        if status == 200:
            assert await client.json_request("GET", "/memes/test/preview") == {"image_id": ID}
        else:
            with pytest.raises(BridgeError):
                await client.json_request("GET", "/memes/test/preview")
        assert not client.gate.blocked and client.gate.active == 0
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,body", [(502, b"gateway failed"), (200, b"broken JSON"), (200, b'{"image_id":"invalid"}')]
)
async def test_unverified_or_proxy_response_trips_even_with_headers(status, body):
    client = MemeClient(settings(), httpx.MockTransport(lambda r: httpx.Response(status, content=body)))
    try:
        try:
            await client.preview("test")
        except BridgeError:
            pass
        assert client.gate.blocked and client.gate.active == 0
    finally:
        await client.close()


async def test_body_timeout_after_headers_remains_unknown_and_trips():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"image_id":'
            raise httpx.ReadTimeout("body stalled")

    client = MemeClient(settings(), httpx.MockTransport(lambda r: httpx.Response(200, stream=BrokenStream())))
    try:
        with pytest.raises(BridgeError, match="超时"):
            await client.preview("test")
        assert client.gate.blocked
    finally:
        await client.close()


async def test_nonheavy_timeout_does_not_trip_and_config_rollback_cannot_recover():
    client = MemeClient(settings(), httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("timeout"))))
    try:
        with pytest.raises(BridgeError):
            await client.metadata()
        assert not client.gate.blocked
        gate = client.gate
        gate.configure(3)
        gate.blocked = True
        gate.configure(2)
        gate.configure(3)
        assert gate.blocked and gate.epoch == 3
        gate.configure(4)
        assert not gate.blocked
    finally:
        await client.close()


async def test_sending_or_downloading_known_image_does_not_mislabel_server_as_running():
    def handler(request):
        if request.url.path.endswith("/preview"):
            return httpx.Response(200, json={"image_id": ID})
        raise httpx.ReadTimeout("download timeout after rendering")

    client = MemeClient(settings(), httpx.MockTransport(handler))
    try:
        with pytest.raises(BridgeError):
            await client.preview("test")
        assert not client.gate.blocked
    finally:
        await client.close()


async def test_trip_race_recovery_requires_all_local_leases_exit():
    gate = HeavyGate()
    gate.configure(0)
    with gate.operation(2) as first:
        with pytest.raises(httpx.ReadTimeout):
            with gate.operation(2):
                raise httpx.ReadTimeout("timeout")
        assert gate.blocked and gate.active == 1
        with pytest.raises(BridgeError, match="尚未退出"):
            gate.configure(1)
        with pytest.raises(BridgeError, match="熔断"):
            with gate.operation(2):
                pass
        first[0] = True
    gate.configure(0)
    assert gate.blocked
    gate.configure(1)
    assert not gate.blocked and gate.active == 0
    with gate.operation(1) as complete:
        with pytest.raises(BridgeError, match="并发"):
            with gate.operation(1):
                pass
        complete[0] = True


async def test_unload_cancellation_and_same_epoch_reload_preserve_circuit():
    p = await plugin()
    entered = asyncio.Event()

    async def handler(request):
        if request.url.path == "/meme/infos":
            return httpx.Response(200, json=[ITEM])
        entered.set()
        await asyncio.Event().wait()

    await p.client.close()
    p.client = MemeClient(p.config.plugin, httpx.MockTransport(handler))
    p.client.gate = p._heavy_gate
    old_cleanup = p._cleanup_task
    task = asyncio.create_task(p.handle_command(**args("/表情预览 test")))
    await entered.wait()
    config = p.get_plugin_config_data()
    await p.on_config_update("self", config, "1.2.0")
    try:
        assert task.cancelled() and old_cleanup.cancelled()
        assert p.client.gate is p._heavy_gate and p.client.gate.blocked and not p.client.gate.active
        with pytest.raises(BridgeError, match="熔断"):
            await p.client.preview("test")
        config["plugin"]["recovery_epoch"] = 1
        await p.on_config_update("self", config, "1.2.0")
        assert not p.client.gate.blocked
    finally:
        await p.on_unload()
    assert p._cleanup_task is None


async def test_unload_during_initial_refresh_cannot_revive_background_tasks(monkeypatch):
    import maibot_plugin_meme_generator_bridge.plugin as module

    p = await plugin()
    await p.on_unload()
    config = p.get_plugin_config_data()
    config["keywords"]["enabled"] = True
    p.set_plugin_config(config)
    entered, release = asyncio.Event(), asyncio.Event()

    async def metadata(force=False):
        entered.set()
        await release.wait()
        return [ITEM]

    def client(config):
        instance = MemeClient(config, httpx.MockTransport(lambda r: httpx.Response(200, json=[ITEM])))
        instance.metadata = metadata
        return instance

    monkeypatch.setattr(module, "MemeClient", client)
    loading = asyncio.create_task(p.on_load())
    await entered.wait()
    await p.on_unload()
    release.set()
    await loading
    try:
        assert p.client.http.is_closed
        assert p._cleanup_task is None and p._refresh_task is None and not p._index.names
    finally:
        await p.on_unload()


@pytest.mark.parametrize(
    "config,texts,images,match",
    [
        ({"max_total_input_bytes": 1024}, ["x"], [PNG + b"a" * 600, PNG + b"b" * 600], "原始字节总量"),
        ({"max_total_text_chars": 3}, ["aa", "bb"], [PNG], "总字符"),
        ({"max_total_text_bytes": 5}, ["中文"], [], "UTF-8"),
        ({"max_request_bytes": 1024}, ["中" * 400], [], "HTTP JSON"),
        ({"max_request_bytes": 1024}, ["x"], [PNG + b"x" * 800], "Base64"),
        ({"max_operation_request_bytes": 1024}, ["x"], [PNG + b"x" * 350, PNG + b"y" * 350], "累计"),
        ({}, ["x"], [PNG, b"not an image"], "图片"),
        ({}, ["\ud800"], [], "Unicode"),
    ],
)
async def test_generation_preflights_all_data_before_any_upload(config, texts, images, match):
    calls = []
    client = MemeClient(settings(**config), httpx.MockTransport(lambda r: calls.append(r)))
    try:
        with pytest.raises(BridgeError, match=match):
            await client.generate(ITEM, texts, images)
        assert not calls and not client.gate.blocked
    finally:
        await client.close()


@pytest.mark.parametrize("padding", [799, 800, 801])
async def test_exact_utf8_base64_and_final_json_boundary(padding):
    item = copy.deepcopy(ITEM)
    text = '中文"\\\n'
    sample = PNG + b"x" * padding
    uploads = [{"type": "data", "data": base64.b64encode(sample).decode()}]
    final = {"images": [{"name": "image1", "id": ID}], "texts": [text], "options": {}}
    sizes = [len(json_body(x)) for x in [*uploads, final]]
    calls = []

    def handler(request):
        if request.method == "POST":
            calls.append(request.content)
            return httpx.Response(200, json={"image_id": ID})
        return httpx.Response(200, content=PNG)

    client = MemeClient(settings(), httpx.MockTransport(handler))
    # Exercise exact boundaries without allocating megabytes in every test.
    client.settings.max_request_bytes = max(sizes)
    client.settings.max_operation_request_bytes = sum(sizes)
    client.settings.max_total_text_bytes = len(text.encode())
    try:
        assert await client.generate(item, [text], [sample]) == (PNG, "png")
        assert calls == [json_body(uploads[0]), json_body(final)]
        assert b"\xe4\xb8\xad" in calls[1] and b"\\n" in calls[1]
        calls.clear()
        client.settings.max_operation_request_bytes -= 1
        with pytest.raises(BridgeError, match="累计"):
            await client.generate(item, [text], [sample])
        assert not calls
    finally:
        await client.close()


async def test_zero_inputs_remain_supported_for_template_that_allows_them():
    item = copy.deepcopy(ITEM)
    item["params"]["min_texts"] = 0
    client = MemeClient(
        settings(),
        httpx.MockTransport(
            lambda r: (
                httpx.Response(200, content=PNG) if r.method == "GET" else httpx.Response(200, json={"image_id": ID})
            )
        ),
    )
    try:
        assert await client.generate(item, [], []) == (PNG, "png")
    finally:
        await client.close()


async def test_active_cache_tick_with_controllable_monotonic_clock():
    p = await plugin(cache_cleanup_interval=1)
    clock = [10.0]
    p._clock = lambda: clock[0]
    p.avatars._clock = p._clock
    p._media[("s", "m")] = (20, [(0, "image", "h", "encoded")])
    p.avatars._cache["old"] = (20, PNG)
    p.avatars._bytes = len(PNG)
    clock[0] = 30
    try:
        await asyncio.sleep(1.05)
        assert not p._media and not p.avatars._cache and p.avatars._bytes == 0
        p._media[("s", "m")] = (40, [])
        p.avatars._cache["new"] = (40, PNG)
        p.avatars._bytes = len(PNG)
        p._purge_media()
        p.avatars.purge_expired()
        assert p._media and p.avatars._bytes == len(PNG)
        clock[0] = 40
        p._purge_media()
        p.avatars.purge_expired()
        p.avatars.purge_expired()
        assert not p._media and not p.avatars._cache and p.avatars._bytes == 0
    finally:
        await p.on_unload()


async def test_avatar_close_cannot_be_repopulated_by_inflight_download():
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingPool(FakePool):
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            entered.set()
            await release.wait()
            yield self.response

    avatar = AvatarDownloader(AvatarSettings(), 1024, WaitingPool(FakeResponse([PNG])))
    task = asyncio.create_task(avatar.get("https://q1.qlogo.cn/g?b=qq&nk=12345&s=640", "12345"))
    await entered.wait()
    await avatar.close()
    release.set()
    with pytest.raises(BridgeError, match="关闭"):
        await task
    assert not avatar._cache and avatar._bytes == 0


async def test_avatar_tick_while_new_value_downloads_keeps_correct_accounting():
    clock = [10.0]
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingPool(FakePool):
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            entered.set()
            await release.wait()
            yield self.response

    url = "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"
    avatar = AvatarDownloader(
        AvatarSettings(cache_seconds=1), 1024, WaitingPool(FakeResponse([PNG])), clock=lambda: clock[0]
    )
    avatar._cache[url] = (9, PNG)
    avatar._bytes = len(PNG)
    task = asyncio.create_task(avatar.get(url, "12345"))
    await entered.wait()
    avatar.purge_expired()
    assert avatar._bytes == 0
    clock[0] = 20
    release.set()
    assert await task == PNG
    avatar.purge_expired()
    assert avatar._bytes == len(PNG) and avatar._cache[url][0] == 21
    clock[0] = 21
    avatar.purge_expired()
    assert avatar._bytes == 0
    await avatar.close()


def test_new_ui_limits_roundtrip_and_validation():
    from pydantic import ValidationError
    from maibot_plugin_meme_generator_bridge.config import Settings
    from maibot_plugin_meme_generator_bridge.plugin import create_plugin

    p = create_plugin()
    fields = p.get_webui_config_schema()["sections"]["plugin"]["fields"]
    for name in (
        "recovery_epoch",
        "max_input_mb",
        "max_total_input_mb",
        "max_request_mb",
        "max_operation_request_mb",
        "max_output_mb",
        "max_metadata_mb",
        "max_total_text_chars",
        "max_total_text_kb",
        "cache_cleanup_interval",
    ):
        assert fields[name]["label"] and not fields[name]["hidden"]
    with pytest.raises(ValidationError):
        Settings(recovery_epoch=-1)
    with pytest.raises(ValidationError):
        Settings(cache_cleanup_interval=0)
