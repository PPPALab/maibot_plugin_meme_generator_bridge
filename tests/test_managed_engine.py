"""No upstream program execution: tiny assets, mock HTTP and our own Python child."""

from types import ModuleType, SimpleNamespace

import asyncio
import hashlib
import io
import json
import os
import signal
import socket
import stat
import sys
import threading
import time
import zipfile

import httpx
import pytest

from test_bridge import ITEM, plugin

from maibot_plugin_meme_generator_bridge import engine as runtime
from maibot_plugin_meme_generator_bridge import engine_install as installer
from maibot_plugin_meme_generator_bridge.client import BridgeError, MemeClient
from maibot_plugin_meme_generator_bridge.config import BridgeConfig, Settings
from maibot_plugin_meme_generator_bridge.engine_install import EngineError


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for offset in range(0, len(self.data), 17):
            yield self.data[offset : offset + 17]


def resource_zip(files, manifest):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(installer.RESOURCE_ROOT, b"")
        bundle.writestr(installer.RESOURCE_ROOT + "resources/", b"")
        bundle.writestr(installer.RESOURCE_ROOT + "resources/resources.json", manifest)
        for name, data in files.items():
            bundle.writestr(installer.RESOURCE_ROOT + "resources/" + name, data)
    return buffer.getvalue()


@pytest.fixture
def assets(monkeypatch):
    elf = bytearray(64)
    elf[:6] = b"\x7fELF\x02\x01"
    elf[18:20] = (62).to_bytes(2, "little")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("meme", elf)
    archive = buffer.getvalue()
    files = {"fonts/test.ttf": b"synthetic font", "images/example/0.png": b"synthetic image"}
    manifest = json.dumps(
        {
            group: [
                {"file": name.split("/", 1)[1], "hash": sha(data)}
                for name, data in files.items()
                if name.startswith(group + "/")
            ]
            for group in ("fonts", "images")
        }
    ).encode()
    monkeypatch.setattr(installer, "RESOURCE_SHA256", sha(manifest))
    monkeypatch.setitem(installer.ASSETS, "linux-x86_64", (len(archive), sha(archive)))
    source = resource_zip(files, manifest)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        assert str(request.url) in {
            installer.RELEASE_BASE + "meme-generator-cli-linux-x86_64.zip",
            installer.RESOURCE_ARCHIVE,
        }
        data = source if str(request.url) == installer.RESOURCE_ARCHIVE else archive
        return httpx.Response(200, stream=Chunks(data))

    return httpx.MockTransport(respond), calls


@pytest.mark.parametrize(
    "machine,expected",
    [("x86_64", "linux-x86_64"), ("AMD64", "linux-x86_64"), ("aarch64", "linux-aarch64"), ("arm64", "linux-aarch64")],
)
def test_platform_mapping(machine, expected):
    assert installer.select_target("linux", machine, "glibc 2.36", 64) == expected


@pytest.mark.parametrize(
    "system,machine,libc,bits",
    [
        ("linux", "x86_64", "musl 1.2", 64),
        ("linux", "x86_64", "", 64),
        ("win32", "arm64", "", 64),
        ("win32", "x86", "", 32),
        ("darwin", "ppc64", "", 64),
        ("android", "x86_64", "", 64),
        ("linux", "i686", "glibc 2.36", 32),
        ("linux", "riscv64", "glibc 2.36", 64),
    ],
)
def test_unsupported_rejected(system, machine, libc, bits):
    with pytest.raises(EngineError):
        installer.select_target(system, machine, libc, bits)


def test_libc_probe_error_is_explicitly_unsupported(monkeypatch):
    def unavailable(name):
        raise ValueError("unsupported confstr")

    monkeypatch.setattr(installer.os, "confstr", unavailable)
    with pytest.raises(EngineError, match="musl"):
        installer.detect_target()


async def test_default_disabled_and_widgets_zero_side_effects(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled engine touched environment")

    monkeypatch.setattr(runtime, "acquire", forbidden)
    monkeypatch.setattr(runtime, "detect_target", forbidden)
    manager = runtime.EngineManager(tmp_path / "absent")
    manager.configure(False, 22331)
    manager.retry()
    await manager.close()
    assert not (tmp_path / "absent").exists()
    p = await plugin()
    try:
        for _ in range(3):
            snapshot = await p.get_config_widget_state()
            assert snapshot["engine_retry"].disabled
            await p.on_config_widget_action("engine_retry")
        assert p._engine is None
        assert BridgeConfig().managed.enabled is False
    finally:
        await p.on_unload()


async def test_atomic_install_reuse_and_tamper_refusal(tmp_path, assets):
    transport, calls = assets
    phases = []
    root = tmp_path / "engine"
    root.mkdir()
    home = await installer.install(root, "linux-x86_64", lambda phase, text: phases.append(phase), transport)
    assert len(calls) == 2 and {"下载", "校验", "资源"} <= set(phases)
    assert not list(root.glob(".staging*"))
    assert not (home / "source.zip").exists()
    assert sorted(p.name for p in home.iterdir()) == [
        "LICENSE.upstream.txt",
        "engine.zip",
        "meme",
        "resources",
        "resources.json",
    ]
    again = await installer.install(root, "linux-x86_64", lambda *args: None, transport)
    assert home == again and len(calls) == 2
    (home / "resources/fonts/test.ttf").write_bytes(b"tampered")
    with pytest.raises(EngineError, match="素材校验失败"):
        await installer.install(root, "linux-x86_64", lambda *args: None, transport)
    assert len(calls) == 2


async def test_failed_hash_leaves_no_partial_and_can_retry(tmp_path, assets):
    transport, _ = assets
    broken = httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks(b"bad")))
    with pytest.raises(EngineError, match="SHA256"):
        await installer.install(tmp_path, "linux-x86_64", lambda *args: None, broken)
    assert not list(tmp_path.iterdir())
    home = await installer.install(tmp_path, "linux-x86_64", lambda *args: None, transport)
    assert (home / "meme").exists()


@pytest.mark.parametrize(
    "name,mode",
    [
        ("../meme", 0),
        ("/meme", 0),
        ("a/../../meme", 0),
        ("C:/meme", 0),
        ("a\\meme", 0),
        ("meme", stat.S_IFLNK | 0o777),
        ("meme", stat.S_IFIFO | 0o600),
        ("extra.so", 0),
    ],
)
def test_unsafe_zip_rejected(tmp_path, name, mode):
    archive = tmp_path / "engine.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        bundle.writestr(info, b"x")
    with pytest.raises(EngineError):
        installer.extract(archive, tmp_path)
    assert not (tmp_path.parent / "meme").exists()


def test_duplicate_zip_rejected(tmp_path):
    archive = tmp_path / "engine.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("meme", b"x")
        with pytest.warns(UserWarning):
            bundle.writestr("meme", b"x")
    with pytest.raises(EngineError, match="重复"):
        installer.extract(archive, tmp_path)


def test_declared_zip_expansion_budget(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("meme", b"tiny")
    data = bytearray(buffer.getvalue())
    central = data.index(b"PK\x01\x02")
    data[central + 24 : central + 28] = (128_000_001).to_bytes(4, "little")
    archive = tmp_path / "large.zip"
    archive.write_bytes(data)
    with pytest.raises(EngineError, match="解压大小"):
        installer.extract(archive, tmp_path)
    assert not (tmp_path / "meme").exists()


async def test_cached_resource_ancestor_link_rejected(tmp_path, assets):
    home = await installer.install(tmp_path, "linux-x86_64", lambda *args: None, assets[0])
    fonts = home / "resources/fonts"
    fonts.rename(home / "original-fonts")
    fonts.symlink_to(home / "original-fonts", target_is_directory=True)
    with pytest.raises(EngineError, match="链接"):
        await installer.disk(installer.verify_install, home, "linux-x86_64")


def test_manifest_hash_is_trust_anchor():
    with pytest.raises(EngineError, match="清单 SHA256"):
        installer.resource_entries(b'{"fonts": [], "images": []}')


def test_disk_budget_noexec_and_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda root: SimpleNamespace(free=1))
    with pytest.raises(EngineError, match="空间不足"):
        installer.check_space(tmp_path)
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda root: SimpleNamespace(free=2_000_000_000))
    monkeypatch.setattr(installer.os, "statvfs", lambda root: SimpleNamespace(f_flag=8))
    with pytest.raises(EngineError, match="noexec"):
        installer.check_space(tmp_path)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(EngineError, match="链接"):
        installer.secure_directory(tmp_path / "link" / "child")


async def test_download_bounds_redirect_and_hash(tmp_path):
    scenarios = [
        (httpx.Response(302, headers={"location": "http://127.0.0.1/private"}), "目标"),
        (httpx.Response(200, stream=Chunks(b"four")), "预算"),
        (httpx.Response(200, stream=Chunks(b"bad")), "SHA256"),
    ]
    for index, (response, match) in enumerate(scenarios):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            with pytest.raises(EngineError, match=match):
                await installer.download(client, installer.RESOURCE_ARCHIVE, tmp_path / str(index), sha(b"yes"), 3)


async def test_cancel_download_cleanup_and_retry(tmp_path, assets):
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(request):
        entered.set()
        await release.wait()
        return httpx.Response(200)

    task = asyncio.create_task(
        installer.install(tmp_path, "linux-x86_64", lambda *args: None, httpx.MockTransport(waiting))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.iterdir())
    await installer.install(tmp_path, "linux-x86_64", lambda *args: None, assets[0])


async def test_cancel_joins_disk_worker_and_closes_result():
    entered, release = threading.Event(), threading.Event()
    closed = []

    def slow():
        entered.set()
        release.wait(2)
        return 42

    task = asyncio.create_task(installer.disk(slow, on_cancel=closed.append))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [42]


def test_owner_lock_and_port_conflict(tmp_path):
    fd = runtime.acquire(tmp_path)
    try:
        with pytest.raises(EngineError, match="持有锁"):
            runtime.acquire(tmp_path)
    finally:
        os.close(fd)
    fd = runtime.acquire(tmp_path)
    os.close(fd)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(EngineError, match="占用"):
            runtime.check_port(listener.getsockname()[1])
        assert listener.fileno() >= 0


async def test_effective_url_never_falls_back():
    settings = Settings(base_url="http://external.invalid")
    calls = []
    client = MemeClient(
        settings, httpx.MockTransport(lambda request: (calls.append(str(request.url)), httpx.Response(200, json=[]))[1])
    )
    current = None
    client.endpoint = lambda: current
    try:
        with pytest.raises(BridgeError, match="尚未就绪"):
            await client.request("GET", "/meme/infos", 100)
        assert not calls
        current = "http://127.0.0.1:22331"
        await client.request("GET", "/meme/infos", 100)
        assert calls == [current + "/meme/infos"] and settings.base_url == "http://external.invalid"
        client.endpoint = None
        await client.request("GET", "/meme/infos", 100)
        assert calls[-1].startswith(settings.base_url)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "version,infos,valid",
    [(b"0.2.3", [ITEM], True), (b"0.2.2", [ITEM], False), (b"0.2.3", [], False), (b"0.2.3", {"key": "x"}, False)],
)
async def test_health_contract(version, infos, valid):
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(200, content=version)
            if request.url.path.endswith("version")
            else httpx.Response(200, json=infos)
        )
    )
    if valid:
        await runtime.health("http://127.0.0.1:22331", transport)
    else:
        with pytest.raises(EngineError):
            await runtime.health("http://127.0.0.1:22331", transport)


async def wait_phase(manager, phase):
    async def waiting():
        while manager.phase != phase:
            if manager.phase == "失败":
                raise AssertionError(manager.detail)
            await asyncio.sleep(0.01)

    await asyncio.wait_for(waiting(), 5)


async def test_background_save_retry_and_unload_preserve_gate(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def installing(root, target, status):
        calls.append(root)
        entered.set()
        await release.wait()
        raise EngineError("合成安装失败")

    monkeypatch.setattr(runtime, "install", installing)
    p = await plugin()
    p.ctx.paths = SimpleNamespace(data_dir=tmp_path)
    config = p.get_plugin_config_data()
    config["managed"] = {"enabled": True, "port": 22331}
    original_url = config["plugin"]["base_url"]
    try:
        await asyncio.wait_for(p.on_config_update("self", config, "1.6.0"), 0.5)
        await entered.wait()
        engine = p._engine
        p._heavy_gate.blocked = True
        await asyncio.gather(*(p.on_config_update("self", config, "1.6.0") for _ in range(4)))
        assert p._engine is engine and len(calls) == 1 and p._heavy_gate.blocked
        assert p.config.plugin.base_url == original_url
        for _ in range(3):
            await asyncio.wait_for(p.get_config_widget_state(), 0.1)
        assert len(calls) == 1
        release.set()
        while engine.phase != "失败":
            await asyncio.sleep(0.01)
        await p.on_config_widget_action("engine_retry")
        assert len(calls) == 1  # backoff enforced, not just UI disabled
        engine.retry_after = 0
        await asyncio.wait_for(p.on_config_widget_action("engine_retry"), 0.1)
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        config["managed"]["enabled"] = False
        await asyncio.wait_for(p.on_config_update("self", config, "1.6.0"), 0.5)
        assert p.client.endpoint is None
        await wait_phase(engine, "关闭")
        assert (tmp_path / "managed-engine/owner.lock").exists()
    finally:
        await p.on_unload()
    assert engine._supervisor.done() and engine.process is None
    assert p._engine is None


@pytest.mark.parametrize("guarded", [False, True])
async def test_owned_harmless_child_ready_drain_stop_and_retained_data(tmp_path, monkeypatch, guarded):
    # Our own tiny HTTP fixture, not an upstream executable or downloaded asset.
    home = tmp_path / "fixture"
    home.mkdir()
    script = home / "meme"
    script.write_text(
        f"#!{sys.executable}\n"
        + """
import http.server, json, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = b"0.2.3" if self.path == "/meme/version" else b'[{"key":"fixture"}]'
        self.send_response(200); self.end_headers(); self.wfile.write(data)
    def log_message(self, *args): pass
sys.stdout.write("x" * 200000); sys.stdout.flush()
http.server.HTTPServer(("127.0.0.1", int(sys.argv[-1])), Handler).serve_forever()
"""
    )
    script.chmod(0o700)

    async def installed(*args):
        return home

    monkeypatch.setattr(runtime, "install", installed)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    manager = runtime.EngineManager(tmp_path / "data")
    manager._guarded = guarded  # Exercise the portable helper on Linux, not a macOS claim.
    try:
        manager.configure(True, port)
        await wait_phase(manager, "就绪")
        proc = manager.process
        assert runtime.owns_listener(manager._engine_pid, port)
        assert manager.url == f"http://127.0.0.1:{port}"
        assert sum(map(len, manager.logs)) <= 65536
        for _ in range(5):
            manager.configure(True, port)
        await asyncio.sleep(0)
        assert manager.process is proc
        manager.configure(False, port)
        assert manager.url is None
        await wait_phase(manager, "关闭")
        assert proc.returncode is not None and script.exists()
    finally:
        await manager.close()


async def test_start_failure_backoff_and_no_restart(tmp_path, monkeypatch):
    home = tmp_path / "bad"
    home.mkdir()
    (home / "meme").write_text("not executable")

    async def installed(*args):
        return home

    monkeypatch.setattr(runtime, "install", installed)
    manager = runtime.EngineManager(tmp_path / "data")
    try:
        manager.configure(True, 22331)

        async def failed():
            while manager.phase != "失败":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(failed(), 3)
        assert manager.url is None and manager.retry_after > time.monotonic()
        worker = manager._worker
        manager.configure(True, 22331)
        manager.retry()
        assert manager._worker is worker
    finally:
        await manager.close()


async def test_conflicting_service_never_downloaded_or_killed(tmp_path, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("port collision must precede installation")

    monkeypatch.setattr(runtime, "install", forbidden)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        manager = runtime.EngineManager(tmp_path)
        try:
            manager.configure(True, listener.getsockname()[1])

            async def failed():
                while manager.phase != "失败":
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(failed(), 2)
            assert "占用" in manager.detail and manager.process is None
            assert listener.fileno() >= 0 and manager.url is None
        finally:
            await manager.close()


async def test_lock_inherited_by_child_survives_parent_fd_close(tmp_path):
    fd = runtime.acquire(tmp_path)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import sys; sys.stdin.buffer.read(1)",
        stdin=asyncio.subprocess.PIPE,
        pass_fds=(fd,),
    )
    os.close(fd)
    try:
        with pytest.raises(EngineError, match="持有锁"):
            runtime.acquire(tmp_path)
        child.stdin.write(b"x")
        await child.stdin.drain()
        await asyncio.wait_for(child.wait(), 2)
        next_fd = runtime.acquire(tmp_path)
        os.close(next_fd)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_linux_parent_death_releases_inherited_lock(tmp_path):
    # Both parent and child are our own stdlib fixtures. Simulate a killed
    # Runner and prove the inherited owner lock is eventually released.
    parent_code = f"""
import fcntl, os, subprocess, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
child = subprocess.Popen([
    sys.executable, "-I", "-S", "-c", {runtime.CHILD_BOOTSTRAP!r}, str(os.getpid()),
    sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"
], pass_fds=(fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
time.sleep(30)
"""
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        parent_code,
        str(tmp_path / "owner.lock"),
        stdout=asyncio.subprocess.PIPE,
    )
    child_pid = None
    released = False
    try:
        child_pid = int(await asyncio.wait_for(parent.stdout.readline(), 2))
        with pytest.raises(EngineError, match="持有锁"):
            runtime.acquire(tmp_path)
        parent.kill()
        await asyncio.wait_for(parent.wait(), 2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                fd = runtime.acquire(tmp_path)
            except EngineError:
                await asyncio.sleep(0.01)
            else:
                os.close(fd)
                released = True
                break
        assert released
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        if child_pid and not released:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_unload_during_install_cleans_worker_and_unlocks(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def installing(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runtime, "install", installing)
    manager = runtime.EngineManager(tmp_path)
    manager.configure(True, 22331)
    await entered.wait()
    await manager.close()
    assert cancelled.is_set() and manager._supervisor.done() and manager.process is None
    fd = runtime.acquire(manager.root)
    os.close(fd)


async def test_host_widget_protocol_and_confirmation():
    # Execute the official Host's standalone widget contract without importing
    # or booting its application/runtime configuration.
    from host_dispatch import host_root

    path = host_root() / "src/plugin_runtime/config_widgets.py"
    module = ModuleType("managed_widget_contract")
    # Compile in memory instead of an import loader that may write a reference
    # repository's __pycache__. No Host package/configuration is imported.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    p = await plugin()
    try:
        declarations = p.get_webui_config_schema()["runtime_widgets"]
        snapshot = await asyncio.wait_for(module.read_widget_snapshot(p, declarations), 0.1)
        assert snapshot.widgets[1].confirmation
        with pytest.raises(ValueError):
            await module.invoke_widget_action(p, snapshot, "engine_retry")
        assert p._engine is None
    finally:
        await p.on_unload()
