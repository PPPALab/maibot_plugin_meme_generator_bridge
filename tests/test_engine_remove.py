import asyncio
import os

import pytest

from maibot_plugin_meme_generator_bridge import engine


def test_remove_preserves_lock_and_other_plugin_data(tmp_path):
    root = tmp_path / "managed-engine"
    root.mkdir()
    (root / "v0.2.3-linux-x86_64").mkdir()
    (root / "v0.2.3-linux-x86_64" / "engine.zip").write_bytes(b"archive")
    (tmp_path / "other.txt").write_text("keep")
    fd = engine.acquire(root)
    try:
        with pytest.raises(engine.EngineError):
            engine.remove_data(root)
        assert (root / "v0.2.3-linux-x86_64" / "engine.zip").exists()
    finally:
        os.close(fd)
    inode = (root / "owner.lock").stat().st_ino
    engine.remove_data(root)
    assert list(root.iterdir()) == [root / "owner.lock"]
    assert (root / "owner.lock").stat().st_ino == inode
    assert (tmp_path / "other.txt").read_text() == "keep"


def test_remove_rejects_linked_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    root = tmp_path / "managed-engine"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(engine.EngineError):
        engine.remove_data(root)
    assert (outside / "keep").exists()


async def test_background_remove_and_enabled_rejection(tmp_path):
    manager = engine.EngineManager(tmp_path)
    manager.root.mkdir()
    (manager.root / "engine.zip").write_bytes(b"archive")
    manager.desired = 22331
    with pytest.raises(engine.EngineError):
        manager.remove()
    manager.desired = None
    manager.remove()
    assert manager.phase == "清理"
    for _ in range(200):
        if manager.phase != "清理":
            break
        await asyncio.sleep(0.01)
    assert manager.phase == "关闭"
    assert "已删除" in manager.detail
    assert not (manager.root / "engine.zip").exists()
    await manager.close()


async def test_enable_supersedes_queued_removal(tmp_path, monkeypatch):
    manager = engine.EngineManager(tmp_path)
    manager.root.mkdir()
    archive = manager.root / "engine.zip"
    archive.write_bytes(b"keep")
    started = asyncio.Event()

    async def run(port):
        started.set()

    monkeypatch.setattr(manager, "_run", run)
    manager.remove()
    manager.configure(True, 22331)
    await asyncio.wait_for(started.wait(), 1)
    assert archive.read_bytes() == b"keep"
    await manager.close()
