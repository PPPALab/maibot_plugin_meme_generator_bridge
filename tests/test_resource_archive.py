"""Archive trust boundaries, cancellation and the actual status-widget contract."""

from pathlib import Path
from types import SimpleNamespace

import asyncio
import io
import json
import os
import stat
import struct
import threading
import zipfile

import httpx
import pytest

from test_bridge import plugin
from test_engine_platforms import binary
from test_managed_engine import Chunks, sha

from maibot_plugin_meme_generator_bridge import engine_install as install
from maibot_plugin_meme_generator_bridge import engine_progress as progress_module
from maibot_plugin_meme_generator_bridge.engine import EngineManager, acquire
from maibot_plugin_meme_generator_bridge.engine_progress import InstallProgress


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries:
            bundle.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def source_entries(monkeypatch):
    manifest = json.dumps({"fonts": [], "images": [{"file": "test.png", "hash": sha(b"image")}]}).encode()
    engine = make_zip([("meme", binary("linux-x86_64"))])
    monkeypatch.setitem(install.ASSETS, "linux-x86_64", (len(engine), sha(engine)))
    monkeypatch.setattr(install, "RESOURCE_SHA256", sha(manifest))
    return engine, [
        (install.RESOURCE_ROOT, b""),
        (install.RESOURCE_ROOT + "resources/resources.json", manifest),
        (install.RESOURCE_ROOT + "resources/images/test.png", b"image"),
    ]


def transport(engine, source):
    def respond(request):
        data = source if str(request.url) == install.RESOURCE_ARCHIVE else engine
        return httpx.Response(200, stream=Chunks(data))

    return httpx.MockTransport(respond)


async def test_only_pinned_resources_materialized_ignoring_upstream_readme_symlinks(tmp_path, source_entries):
    engine, entries = source_entries
    link = zipfile.ZipInfo(install.RESOURCE_ROOT + "meme_generator_node/README.md")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    entries += [(link, b"../README.md"), (install.RESOURCE_ROOT + "evil.py", b"never executed")]
    home = await install.install(tmp_path, "linux-x86_64", lambda *a: None, transport(engine, make_zip(entries)))
    assert (home / "resources/images/test.png").read_bytes() == b"image"
    assert not (home / "source.zip").exists()
    assert not list(home.rglob("README.md")) and not list(home.rglob("*.py"))


def test_manifest_inflate_is_bounded_before_hash_check(tmp_path, source_entries, monkeypatch):
    archive = tmp_path / "source.zip"
    archive.write_bytes(make_zip(source_entries[1]))
    read = zipfile.ZipExtFile.read

    def bounded(self, n=-1):
        assert 0 < n <= 1_000_001, "Untrusted metadata caused an unbounded inflate"
        return read(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded)
    bundle, raw, entries = install.open_resources(archive)
    bundle.close()
    assert len(entries) == 1 and raw == source_entries[1][1][1]


@pytest.mark.parametrize(
    "case",
    [
        "missing_manifest",
        "wrong_manifest",
        "missing_resource",
        "wrong_resource",
        "duplicate",
        "case_alias",
        "resource_link",
        "ancestor_link",
        "resource_fifo",
        "wrong_type",
        "wrong_root",
        "traversal",
        "nul",
    ],
)
async def test_invalid_resource_archive_cleans_staging_and_can_retry(tmp_path, source_entries, case):
    engine, entries = source_entries
    valid = make_zip(entries)
    if case == "missing_manifest":
        del entries[1]
    elif case == "wrong_manifest":
        entries[1] = (entries[1][0], b"{}")
    elif case == "missing_resource":
        del entries[2]
    elif case == "wrong_resource":
        entries[2] = (entries[2][0], b"tampered")
    elif case == "duplicate":
        entries.append(entries[2])
    elif case == "case_alias":
        entries.append((entries[2][0].replace("test.png", "TEST.png"), b"image"))
    elif case in {"resource_link", "resource_fifo", "wrong_type", "ancestor_link"}:
        name = install.RESOURCE_ROOT + "resources/images" if case == "ancestor_link" else entries[2][0]
        info = zipfile.ZipInfo(name)
        mode = {
            "resource_link": stat.S_IFLNK,
            "ancestor_link": stat.S_IFLNK,
            "resource_fifo": stat.S_IFIFO,
            "wrong_type": stat.S_IFDIR,
        }[case]
        info.external_attr = (mode | 0o700) << 16
        if case == "ancestor_link":
            entries.append((info, b"../../outside"))
        else:
            entries[2] = (info, b"image")
    elif case == "wrong_root":
        entries = [(name.replace(install.RESOURCE_ROOT, "other/"), data) for name, data in entries]
    elif case == "traversal":
        entries.append((install.RESOURCE_ROOT + "resources/../../escape", b"bad"))
    if case == "duplicate":
        with pytest.warns(UserWarning):
            source = make_zip(entries)
    else:
        source = make_zip(entries)
    if case == "nul":
        source = source.replace(b"test.png", b"test\x00png")
    with pytest.raises(install.EngineError):
        await install.install(tmp_path, "linux-x86_64", lambda *a: None, transport(engine, source))
    assert not list(tmp_path.iterdir())
    assert not (tmp_path.parent / "escape").exists()
    await install.install(tmp_path, "linux-x86_64", lambda *a: None, transport(engine, valid))


@pytest.mark.parametrize("case", ["truncated", "zip64", "count", "directory_size", "offset", "extra_entries"])
def test_untrusted_directory_is_bounded_before_zipfile_allocation(tmp_path, source_entries, monkeypatch, case):
    source = bytearray(make_zip(source_entries[1]))
    end = source.rfind(b"PK\x05\x06")
    if case == "truncated":
        del source[-15:]
    elif case == "zip64":
        struct.pack_into("<HH", source, end + 8, 65535, 65535)
    elif case == "count":
        struct.pack_into("<HH", source, end + 8, 5001, 5001)
    elif case == "directory_size":
        struct.pack_into("<I", source, end + 12, 2_000_001)
    elif case == "offset":
        struct.pack_into("<I", source, end + 16, 0)
    else:
        struct.pack_into("<HH", source, end + 8, 2, 2)
    archive = tmp_path / "bad.zip"
    archive.write_bytes(source)

    def forbidden(*a, **kw):
        pytest.fail("ZipFile was allowed to allocate unbounded/unvalidated metadata")

    monkeypatch.setattr(install.zipfile, "ZipFile", forbidden)
    with pytest.raises(install.EngineError):
        install.open_resources(archive)


@pytest.mark.parametrize("comment_size", [0, 40, 65535])
def test_embedded_zip64_cannot_replace_prechecked_directory(tmp_path, monkeypatch, comment_size):
    archive = tmp_path / "source.zip"
    item = zipfile.ZipInfo(install.RESOURCE_ROOT + "README.md")
    item.comment = b"x" * 76
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(item, b"x" * 3_000_000)
        bundle.comment = b"c" * comment_size
    data = bytearray(archive.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    zip64 = end - 76
    # A normal one-entry EOCD passes the old precheck. ZipFile instead uses
    # these ZIP64 records hidden in that entry's comment to read 2.5 MB.
    requested = 2_500_000
    data[zip64:end] = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, requested, zip64 - requested
    ) + struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64, 1)
    archive.write_bytes(data)

    def forbidden(*a, **kw):
        pytest.fail("Untrusted ZIP64 reached ZipFile before its allocation could be bounded")

    monkeypatch.setattr(install.zipfile, "ZipFile", forbidden)
    with pytest.raises(install.EngineError, match="ZIP64"):
        install.open_resources(archive)


async def test_crash_residue_is_reclaimed_before_space_check(tmp_path, source_entries, monkeypatch):
    engine, entries = source_entries
    root = tmp_path / "managed-engine"
    fd = acquire(root)
    staging = root / ".staging-v0.2.3-linux-x86_64"
    staging.mkdir()
    (staging / "source.zip").write_bytes(b"synthetic crash residue")
    (tmp_path / "other-plugin-data").write_bytes(b"keep")
    checked_space = []

    def disk_usage(path):
        free = 1_100_000_000 if staging.exists() else 1_600_000_000
        checked_space.append(free)
        return SimpleNamespace(free=free)

    monkeypatch.setattr(install.shutil, "disk_usage", disk_usage)
    try:
        home = await install.install(root, "linux-x86_64", lambda *a: None, transport(engine, make_zip(entries)))
        assert checked_space == [1_600_000_000]
        assert (home / "resources/images/test.png").read_bytes() == b"image"
        assert not staging.exists() and (root / "owner.lock").is_file()
        assert (tmp_path / "other-plugin-data").read_bytes() == b"keep"
    finally:
        os.close(fd)


async def test_insufficient_space_after_crash_cleanup_still_blocks_download(tmp_path, monkeypatch):
    staging = tmp_path / ".staging-v0.2.3-linux-x86_64"
    staging.mkdir()
    (staging / "source.zip").write_bytes(b"synthetic crash residue")
    monkeypatch.setattr(install.shutil, "disk_usage", lambda path: SimpleNamespace(free=500_000_000))

    def forbidden(request):
        pytest.fail("Insufficient free space must be rejected before any download")

    with pytest.raises(install.EngineError, match="空间不足"):
        await install.install(tmp_path, "linux-x86_64", lambda *a: None, httpx.MockTransport(forbidden))
    assert not staging.exists()


async def test_crash_recovery_refuses_linked_staging(tmp_path, monkeypatch):
    root = tmp_path / "managed-engine"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    staging = root / ".staging-v0.2.3-linux-x86_64"
    staging.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(install.shutil, "disk_usage", lambda path: SimpleNamespace(free=1_600_000_000))

    def forbidden(request):
        pytest.fail("Unsafe staging must be rejected before any download")

    with pytest.raises(install.EngineError, match="暂存目录是链接"):
        await install.install(root, "linux-x86_64", lambda *a: None, httpx.MockTransport(forbidden))
    assert staging.is_symlink() and (outside / "keep").read_bytes() == b"keep"


@pytest.mark.parametrize("case", ["single_file", "total", "compressed", "encrypted", "crc"])
async def test_expansion_compression_and_crc_bounds(tmp_path, source_entries, monkeypatch, case):
    engine, entries = source_entries
    source = bytearray(make_zip(entries))
    central = source.index(b"PK\x01\x02", source.index(b"PK\x01\x02") + 4)
    if case == "single_file":
        struct.pack_into("<I", source, central + 24, install.RESOURCE_FILE_LIMIT + 1)
    elif case == "total":
        monkeypatch.setattr(install, "RESOURCE_TOTAL_LIMIT", 1)
    elif case == "compressed":
        monkeypatch.setattr(install, "RESOURCE_ARCHIVE_LIMIT", len(source) - 1)
    elif case == "encrypted":
        struct.pack_into("<H", source, central + 8, 1)
    else:
        struct.pack_into("<I", source, central + 16, 0)
    with pytest.raises((install.EngineError, zipfile.BadZipFile)):
        await install.install(tmp_path, "linux-x86_64", lambda *a: None, transport(engine, bytes(source)))
    assert not list(tmp_path.iterdir())


async def test_cancel_resource_download_closes_stream_and_removes_archive(tmp_path, source_entries):
    engine, entries = source_entries
    entered, closed = asyncio.Event(), asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 262144
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    def respond(request):
        stream = Waiting() if str(request.url) == install.RESOURCE_ARCHIVE else Chunks(engine)
        return httpx.Response(200, stream=stream)

    task = asyncio.create_task(install.install(tmp_path, "linux-x86_64", lambda *a: None, httpx.MockTransport(respond)))
    await asyncio.wait_for(entered.wait(), 2)
    assert next(tmp_path.rglob("source.zip")).stat().st_size == 262144
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert closed.is_set() and not list(tmp_path.iterdir())


async def test_repeated_cancel_joins_extraction_and_staging_removal(tmp_path, source_entries, monkeypatch):
    engine, entries = source_entries
    entered, release = threading.Event(), threading.Event()
    cleaning, allow_clean = threading.Event(), threading.Event()
    real_extract, real_remove = install.extract_resource, install.remove_staging

    def slow_extract(*args):
        entered.set()
        assert release.wait(5)
        return real_extract(*args)

    def slow_remove(path):
        if (path / "source.zip").exists():
            cleaning.set()
            assert allow_clean.wait(5)
        real_remove(path)

    monkeypatch.setattr(install, "extract_resource", slow_extract)
    monkeypatch.setattr(install, "remove_staging", slow_remove)
    task = asyncio.create_task(
        install.install(tmp_path, "linux-x86_64", lambda *a: None, transport(engine, make_zip(entries)))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not cleaning.is_set()
        release.set()
        assert await asyncio.to_thread(cleaning.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        allow_clean.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not list(tmp_path.iterdir())
    finally:
        release.set()
        allow_clean.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "length,body,match",
    [
        ("bad", b"abc", "总大小"),
        ("-1", b"abc", "总大小"),
        ("8", b"abc", "不完整"),
        ("2", b"abc", "大小"),
        ("99", b"abc", "预算"),
    ],
)
async def test_invalid_or_incomplete_content_length(tmp_path, length, body, match):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-length": length}, stream=Chunks(body))
        )
    ) as client:
        with pytest.raises(install.EngineError, match=match):
            await install.download(client, install.RESOURCE_ARCHIVE, tmp_path / "download", None, 10)


async def test_progress_uses_pinned_size_or_real_header_and_freezes_on_completion(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(progress_module, "monotonic", lambda: clock[0])
    progress = InstallProgress("引擎", total=4_000_000)
    progress.update(1_000_000)
    clock[0] = 12.0
    assert "1.00/4.00 MB（25.0%）" in progress.text()
    assert "均速 0.50 MB/s · 耗时 2 秒" in progress.text()
    progress.update(4_000_000)
    assert "99.9%" in progress.text()
    progress.finish()
    completed = progress.text()
    clock[0] = 90.0
    assert progress.text() == completed and "100.0%" in completed
    progress = InstallProgress("素材")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-length": "3"}, stream=Chunks(b"abc"))
        )
    ) as client:
        await install.download(client, install.RESOURCE_ARCHIVE, tmp_path / "source", None, 10, progress)
    assert "100.0%" in progress.text() and progress.total == 3


async def test_widget_elapsed_time_keeps_up_during_stall_without_work(tmp_path, monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(progress_module, "monotonic", lambda: clock[0])
    p = await plugin()
    manager = EngineManager(tmp_path / "absent")
    p._engine = manager
    progress = InstallProgress("素材归档")
    manager.status("资源", progress)
    progress.update(2_000_000)
    tasks = asyncio.all_tasks()
    try:
        clock[0] = 3.0
        text = (await p.get_config_widget_state())["engine_status"].text
        assert "总大小未知，百分比不可用" in text
        assert "2.00 MB" in text and "1.00 MB/s" in text and "耗时 2 秒" in text
        clock[0] = 66.0
        text = (await p.get_config_widget_state())["engine_status"].text
        assert "耗时 1 分 5 秒" in text and "均速 0.03 MB/s" in text
        assert asyncio.all_tasks() == tasks and not manager.root.exists()
        files = InstallProgress("素材解包校验", files_total=3039)
        manager.status("资源", files)
        files.update(400_000_000, 3038)
        assert "3038/3039 个（99.9%）" in manager.detail
        files.update(414_284_402, 3039)
        files.finish()
        assert "100.0%" in manager.detail and "414.28 MB" in manager.detail
        manager.status("关闭", "下载数据已保留")
        assert manager.detail == "下载数据已保留"
    finally:
        await p.on_unload()


async def test_cancellation_during_staging_creation_is_cleaned(tmp_path, source_entries, monkeypatch):
    engine, entries = source_entries
    entered, release = threading.Event(), threading.Event()
    real_directory = install.secure_directory

    def slow_directory(path: Path):
        real_directory(path)
        if path.name.startswith(".staging"):
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(install, "secure_directory", slow_directory)
    task = asyncio.create_task(
        install.install(tmp_path, "linux-x86_64", lambda *a: None, transport(engine, make_zip(entries)))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not list(tmp_path.iterdir())
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
