"""Real cancellation and helper-pipe regressions from the whole-plugin review."""

from pathlib import Path

import asyncio
import os
import signal
import sys
import threading

import pytest

from maibot_plugin_meme_generator_bridge import engine
from maibot_plugin_meme_generator_bridge.engine_guard import GUARD
from maibot_plugin_meme_generator_bridge.engine_install import disk


async def test_repeated_cancel_joins_worker_and_result_cleanup():
    entered, release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()
    closed = []

    def work():
        entered.set()
        assert release.wait(5)
        return 42

    def cleanup(value):
        cleanup_entered.set()
        assert cleanup_release.wait(5)
        closed.append(value)

    task = asyncio.create_task(disk(work, on_cancel=cleanup))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "Repeated cancellation detached the filesystem worker"
        release.set()
        assert await asyncio.to_thread(cleanup_entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "Cancellation detached result/handle cleanup"
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [42]
    finally:
        release.set()
        cleanup_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("guarded", [False, True])
async def test_repeated_cancel_during_spawn_still_reaps_owned_process(tmp_path, monkeypatch, guarded):
    home = tmp_path / "fixture"
    home.mkdir()
    script = home / "meme"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    script.chmod(0o700)
    entered, release = asyncio.Event(), asyncio.Event()
    spawned = []
    real_spawn = asyncio.create_subprocess_exec

    async def installed(*a):
        return home

    async def delayed_spawn(*a, **kw):
        # The OS child already exists, but the caller has not obtained its handle.
        process = await real_spawn(*a, **kw)
        spawned.append(process)
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(engine, "install", installed)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    manager = engine.EngineManager(tmp_path / "data")
    manager._guarded = guarded
    task = asyncio.create_task(manager._run(0))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), "Cancellation detached the in-flight subprocess spawn"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert spawned[0].returncode is not None and manager.process is None
        assert not manager._drains
        fd = engine.acquire(manager.root)
        os.close(fd)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for process in spawned:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await manager.close()


async def test_guard_broken_pid_pipe_reaps_child_and_releases_inherited_lock(tmp_path):
    # Portable guard is exercised on Linux; this is not a macOS/Windows OS claim.
    fd = engine.acquire(tmp_path)
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # PID handshake has no reader, exactly as after Runner exit.
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
        "import time; time.sleep(10)",
        stdin=asyncio.subprocess.PIPE,
        stdout=write_fd,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(fd,),
        start_new_session=True,
    )
    os.close(fd)
    os.close(write_fd)
    try:
        process.stdin.write(b"start\n")
        await process.stdin.drain()
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 5)
        # A leaked engine retains this lock even though the helper has exited.
        fd = engine.acquire(tmp_path)
        os.close(fd)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), 2)


@pytest.mark.parametrize(
    "error,expected",
    [
        (engine.EngineError("托管数据含链接/重解析点，未继续删除，请管理员核实。"), "链接/重解析点"),
        (PermissionError("private-path-and-details"), "PermissionError"),
    ],
)
async def test_remove_keeps_safe_diagnostic(tmp_path, monkeypatch, caplog, error, expected):
    def fail(root: Path) -> None:
        raise error

    monkeypatch.setattr(engine, "remove_data", fail)
    manager = engine.EngineManager(tmp_path)
    manager.remove()
    try:
        for _ in range(100):
            if manager.phase != "清理":
                break
            await asyncio.sleep(0.01)
        assert manager.phase == "失败"
        assert expected in manager.detail
        assert "private-path-and-details" not in manager.detail + caplog.text
        if not isinstance(error, engine.EngineError):
            assert expected in caplog.text
    finally:
        await manager.close()
