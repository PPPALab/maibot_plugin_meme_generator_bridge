"""Platform contracts with synthetic binaries/native API fakes; not OS acceptance."""

from types import SimpleNamespace

import asyncio
import io
import json
import os
import socket
import struct
import sys
import zipfile

import httpx
import pytest

from maibot_plugin_meme_generator_bridge import engine as runtime
from maibot_plugin_meme_generator_bridge import engine_install as install
from maibot_plugin_meme_generator_bridge import engine_windows as windows
from maibot_plugin_meme_generator_bridge.engine_guard import GUARD
from maibot_plugin_meme_generator_bridge.engine_release import ASSETS, RELEASE_BASE

from test_managed_engine import Chunks, resource_zip, sha


TARGETS = [
    ("linux", "x86_64", "glibc 2.35", "linux-x86_64"),
    ("linux", "aarch64", "glibc 2.35", "linux-aarch64"),
    ("win32", "AMD64", "", "windows-x86_64"),
    ("darwin", "x86_64", "", "macos-x86_64"),
    ("darwin", "arm64", "", "macos-aarch64"),
    ("android", "aarch64", "", "android-aarch64"),
]


def binary(target):
    data = bytearray(128)
    if target.startswith("windows"):
        data[:2] = b"MZ"
        data[60:64] = (64).to_bytes(4, "little")
        data[64:70] = b"PE\x00\x00\x64\x86"
        data[88:90] = b"\x0b\x02"
    elif target.startswith("macos"):
        data[:4] = b"\xcf\xfa\xed\xfe"
        data[4:8] = (0x01000007 if target.endswith("x86_64") else 0x0100000C).to_bytes(4, "little")
    else:
        data[:6] = b"\x7fELF\x02\x01"
        data[18:20] = (62 if target.endswith("x86_64") else 183).to_bytes(2, "little")
    return bytes(data)


@pytest.mark.parametrize("system,machine,libc,target", TARGETS)
async def test_all_official_targets_install_from_exact_pinned_url(tmp_path, monkeypatch, system, machine, libc, target):
    assert install.select_target(system, machine, libc, 64) == target
    executable = install.executable_name(target)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(executable, binary(target))
        if target.startswith("windows"):
            bundle.writestr("meme.d", b"Cargo metadata, never executed")
    archive = buffer.getvalue()
    manifest = json.dumps({"fonts": [], "images": [{"file": "test.png", "hash": sha(b"image")}]}).encode()
    source = resource_zip({"images/test.png": b"image"}, manifest)
    monkeypatch.setitem(ASSETS, target, (len(archive), sha(archive)))
    monkeypatch.setattr(install, "RESOURCE_SHA256", sha(manifest))
    calls = []

    def respond(request):
        calls.append(str(request.url))
        data = source if str(request.url) == install.RESOURCE_ARCHIVE else archive
        return httpx.Response(200, stream=Chunks(data))

    home = await install.install(tmp_path, target, lambda *args: None, httpx.MockTransport(respond))
    assert calls == [RELEASE_BASE + f"meme-generator-cli-{target}.zip", install.RESOURCE_ARCHIVE]
    assert (home / executable).read_bytes() == binary(target)
    assert not (home / "meme.d").exists()
    await install.disk(install.verify_install, home, target)
    count = len(calls)
    assert await install.install(tmp_path, target, lambda *args: None, httpx.MockTransport(respond)) == home
    assert len(calls) == count


@pytest.mark.parametrize("target", list(ASSETS))
def test_wrong_binary_format_never_runs(tmp_path, target):
    path = tmp_path / install.executable_name(target)
    path.write_bytes(binary("windows-x86_64" if not target.startswith("windows") else "linux-x86_64"))
    path.chmod(0o700)
    with pytest.raises(install.EngineError):
        install.validate_binary(path, target)


@pytest.mark.parametrize(
    "name", ["../meme", "a/.. /escape", "a./escape", "NUL.txt", "COM1", "a/LPT9.log", "file:stream"]
)
def test_windows_path_normalization_cannot_escape(name):
    with pytest.raises(install.EngineError):
        install.safe_path(name)


def test_reparse_points_are_rejected(monkeypatch, tmp_path):
    class Reparse:
        def lstat(self):
            return SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)

    assert install.is_link(Reparse())


@pytest.mark.parametrize(
    "address,owner,port,expected",
    [
        ("127.0.0.1", 42, 22331, True),
        ("0.0.0.0", 42, 22331, False),
        ("127.0.0.1", 43, 22331, False),
        ("127.0.0.1", 42, 22332, False),
    ],
)
def test_windows_tcp_ownership_rows(address, owner, port, expected):
    row = struct.pack("<6I", 2, struct.unpack("<I", socket.inet_aton(address))[0], socket.htons(port), 0, 0, owner)
    assert windows.table_owns_listener(struct.pack("<I", 1) + row, 42, 22331) is expected
    assert not windows.table_owns_listener(struct.pack("<I", 100) + row, 42, 22331)


@pytest.mark.parametrize("native,machine", [(9, "x86_64"), (12, "aarch64"), (0, "x86"), (999, "unknown")])
def test_windows_native_architecture_not_emulated_python(monkeypatch, native, machine):
    def get_info(buffer):
        struct.pack_into("<H", buffer, 0, native)

    monkeypatch.setattr(windows, "kernel", lambda: SimpleNamespace(GetNativeSystemInfo=get_info))
    assert windows.native_machine() == machine
    if native != 9:
        with pytest.raises(install.EngineError):
            install.select_target("win32", machine, "", 64)


def test_windows_arm64_emulation_cannot_select_x64_asset(monkeypatch):
    def query(handle, process, native):
        process._obj.value = 0x8664
        native._obj.value = 0xAA64
        return True

    api = SimpleNamespace(IsWow64Process2=query, GetCurrentProcess=lambda: -1)
    monkeypatch.setattr(windows, "kernel", lambda: api)
    assert windows.native_machine() == "aarch64"
    with pytest.raises(install.EngineError, match="Windows ARM"):
        install.select_target("win32", windows.native_machine(), "", 64)


def test_windows_job_kill_on_close_and_assignment_failure(monkeypatch):
    calls = []
    api = SimpleNamespace(
        CreateJobObjectW=lambda *args: 100,
        SetInformationJobObject=lambda handle, kind, info, size: calls.append((kind, info._obj.basic.flags)) or True,
        OpenProcess=lambda *args: 200,
        AssignProcessToJobObject=lambda *args: False,
        CloseHandle=lambda handle: calls.append(("close", handle)),
    )
    monkeypatch.setattr(windows, "kernel", lambda: api)
    job = windows.Job()
    with pytest.raises(install.EngineError, match="尚未启动"):
        job.assign(42)
    job.close()
    job.close()
    assert calls == [(9, 0x2000), ("close", 200), ("close", 100)]


@pytest.mark.parametrize(
    "value,valid",
    [
        ("D:P(A;OICI;FA;;;OW)", True),
        ("D:PAI(A;OICI;FA;;;OW)", True),
        ("D:PARAI(A;OICI;0x1f01ff;;;OW)", True),
        ("D:(A;OICI;FA;;;OW)", False),
        ("D:P(A;OICI;FA;;;WD)", False),
        ("D:P(A;OICI;FA;;;OW)(A;;FA;;;WD)", False),
        ("D:P(A;;FA;;;OW)", False),
        ("D:PPAI(A;OICI;FA;;;OW)", False),
    ],
)
def test_windows_private_acl_is_inheritable_owner_only(value, valid):
    assert windows.private_acl_matches(value) is valid


@pytest.mark.parametrize(
    "data,valid",
    [
        (b"p42\nn127.0.0.1:22331\n", True),
        (b"p41\nn127.0.0.1:22331\n", False),
        (b"p42\nn*:22331\n", False),
        (b"p42\nn127.0.0.1:22332\n", False),
    ],
)
def test_macos_listener_requires_own_pid_and_loopback(data, valid):
    assert runtime.macos_listener_output(data, 42, 22331) is valid


async def test_portable_guard_requires_gate_and_stops_child_on_eof(tmp_path):
    # Runs our own stdlib helper/child on Linux, not a macOS/Windows smoke.
    fd = runtime.acquire(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        GUARD,
        str(fd),
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import time; time.sleep(30)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(fd,),
        start_new_session=True,
    )
    try:
        # Parent retains the handle until spawn; helper/child retain it later.
        os.close(fd)
        process.stdin.write(b"start\n")
        await process.stdin.drain()
        info = json.loads(await asyncio.wait_for(process.stdout.readline(), 2))
        assert info["pid"] != process.pid
        with pytest.raises(install.EngineError, match="持有锁"):
            runtime.acquire(tmp_path)
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 6)
        fd = runtime.acquire(tmp_path)
        os.close(fd)
    finally:
        if process.returncode is None:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 6)


async def test_guard_cannot_launch_before_job_assignment_gate(tmp_path):
    marker = tmp_path / "not-created"
    fd = runtime.acquire(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        GUARD,
        str(fd),
        sys.executable,
        "-I",
        "-S",
        "-c",
        f"open({str(marker)!r}, 'w').close()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(fd,),
    )
    os.close(fd)
    process.stdin.close()  # Simulate Runner dying/job assignment failing before start.
    await asyncio.wait_for(process.wait(), 2)
    assert process.returncode == 122 and not marker.exists()
