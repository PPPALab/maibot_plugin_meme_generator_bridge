"""Bounded, pinned installation; no execution, package managers or global changes."""

from pathlib import Path, PurePosixPath
from typing import Any, Callable, List, Optional, Tuple, TypeVar
from urllib.parse import urljoin, urlsplit

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import sys
import zipfile

import httpx

from .engine_progress import InstallProgress, Status
from .engine_release import (
    ASSETS,
    RELEASE_BASE,
    RESOURCE_ARCHIVE,
    RESOURCE_ROOT,
    RESOURCE_SHA256,
    UPSTREAM_LICENSE,
    VERSION,
)

# Peak bounded payload: 450 MB archive + 500 MB resources + 128 MB executable
# + <18 MB engine ZIP + 1 MB manifest; leave headroom inside the 1.2 GB check.
RESOURCE_ARCHIVE_LIMIT = 450_000_000
RESOURCE_TOTAL_LIMIT = 500_000_000
RESOURCE_FILE_LIMIT = 50_000_000


class EngineError(Exception):
    """Safe administrator-facing diagnostic, never raw HTTP response/log content."""


_T = TypeVar("_T")


async def join_task(task: asyncio.Task[_T]) -> Tuple[_T, bool]:
    """Join owned work despite repeated caller cancellation; report deferred cancel."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


async def disk(function: Callable[..., _T], *args: Any, on_cancel: Optional[Callable[[_T], object]] = None) -> _T:
    # Cancellation must join a filesystem worker before deleting its staging tree
    # or releasing its lock. A cancelled to_thread alone does NOT stop that worker.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    result, cancelled = await join_task(task)
    if cancelled:
        if on_cancel is not None:
            await join_task(asyncio.create_task(asyncio.to_thread(on_cancel, result)))
        raise asyncio.CancelledError
    return result


def detect_target() -> str:
    try:
        libc = os.confstr("CS_GNU_LIBC_VERSION") if sys.platform == "linux" else None
    except (OSError, ValueError):
        # musl/Bionic may reject this glibc-specific query. Never guess glibc.
        libc = None
    system = sys.platform
    if (
        system == "linux"
        and not libc
        and (hasattr(sys, "getandroidapilevel") or Path("/system/bin/linker64").is_file())
    ):
        system = "android"
    machine = platform.machine()
    if system == "win32":
        from .engine_windows import native_machine

        machine = native_machine()
    return select_target(system, machine, libc or "", struct.calcsize("P") * 8)


def select_target(system: str, machine: str, libc: str, bits: int) -> str:
    if bits != 64:
        raise EngineError("上游没有匹配的 32 位引擎包。")
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine.lower(), machine.lower())
    if system == "linux" and not re.fullmatch(r"glibc \d+\.\d+", libc):
        raise EngineError("Linux 资产要求 glibc；不能用于 musl/未知运行库。Android 须检测到实际 Android 环境。")
    family = {"linux": "linux", "win32": "windows", "darwin": "macos", "android": "android"}.get(system)
    target = f"{family}-{machine}"
    if target not in ASSETS:
        raise EngineError("上游没有匹配此 OS/CPU 的固定引擎包（不支持 Windows ARM 或未知架构）。")
    return target


def executable_name(target: str) -> str:
    return "meme.exe" if target == "windows-x86_64" else "meme"


def is_link(path: Path) -> bool:
    try:
        info = path.lstat()
        # Windows junctions/reparse points are not all reported as symlinks.
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def safe_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or path.is_absolute()
        or any(p in {"", ".", ".."} for p in name.split("/"))
        or any(
            p.endswith((".", " "))
            or p.split(".")[0].upper()
            in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
            for p in name.split("/")
        )
    ):
        raise EngineError("资源路径不安全。")
    return path


def regular(path: Path) -> None:
    if is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise EngineError("安装文件不是普通文件。")


def secure_directory(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        if is_link(part):
            raise EngineError("托管目录不允许符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise EngineError("托管数据目录不可用。")


def check_space(root: Path, required: int = 1_200_000_000) -> None:
    if shutil.disk_usage(root).free < required:
        raise EngineError("托管安装空间不足，至少需要 1.2 GB 可用空间。")
    if os.name != "nt" and os.statvfs(root).f_flag & getattr(os, "ST_NOEXEC", 8):
        raise EngineError("托管数据目录挂载为 noexec，无法启动程序。")


def digest_file(path: Path, limit: int) -> str:
    regular(path)
    if path.stat().st_size > limit:
        raise EngineError("安装文件超过大小预算。")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(262144), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resource_entries(raw: bytes) -> List[Tuple[str, str]]:
    if len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != RESOURCE_SHA256:
        raise EngineError("资源清单 SHA256 不匹配。")
    data = json.loads(raw)
    if set(data) != {"fonts", "images"}:
        raise EngineError("资源清单结构错误。")
    entries = []
    seen = set()
    for group in ("fonts", "images"):
        for item in data[group]:
            name = f"{group}/{safe_path(item['file'])}"
            if name.casefold() in seen or not re.fullmatch("[0-9a-f]{64}", item["hash"]):
                raise EngineError("资源清单含重复路径或非法哈希。")
            seen.add(name.casefold())
            entries.append((name, item["hash"]))
    if not entries or len(entries) > 4000:
        raise EngineError("资源清单数量超出预算。")
    return entries


async def download(
    client: httpx.AsyncClient,
    url: str,
    path: Path,
    expected: Optional[str],
    limit: int,
    progress: Optional[InstallProgress] = None,
) -> int:
    # Fixed GitHub release/source endpoints only; no arbitrary URL or proxy.
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "codeload.github.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise EngineError("下载重定向目标不在固定来源范围内。")
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise EngineError("下载返回了非预期压缩编码。")
            length = response.headers.get("content-length")
            if length is not None and not re.fullmatch(r"[0-9]{1,12}", length):
                raise EngineError("下载总大小无效。")
            total = int(length) if length is not None else None
            if total is not None and total > limit:
                raise EngineError("下载超过大小预算。")
            if progress is not None:
                if progress.total is not None and total is not None and progress.total != total:
                    raise EngineError("下载总大小与固定资产不匹配。")
                if progress.total is None and total:
                    progress.total = total
            await disk(secure_directory, path.parent)
            handle = await disk(path.open, "xb", on_cancel=lambda handle: handle.close())
            digest, size = hashlib.sha256(), 0
            try:
                async for chunk in response.aiter_raw(262144):
                    size += len(chunk)
                    if size > limit:
                        raise EngineError("下载超过大小预算。")
                    if total is not None and size > total:
                        raise EngineError("下载大小与响应总大小不匹配。")
                    digest.update(chunk)
                    await disk(handle.write, chunk)
                    if progress is not None:
                        progress.update(size)
            finally:
                await disk(handle.close)
            if expected is not None and digest.hexdigest() != expected:
                raise EngineError("下载文件 SHA256 不匹配。")
            if total is not None and size != total:
                raise EngineError("下载不完整，大小与响应总大小不匹配。")
            if progress is not None:
                progress.finish()
            return size
    raise EngineError("下载重定向次数超限。")


def check_resource_zip(archive: Path) -> None:
    """Bound central-directory allocation BEFORE ZipFile reads untrusted metadata."""
    regular(archive)
    size = archive.stat().st_size
    if not 22 <= size <= RESOURCE_ARCHIVE_LIMIT:
        raise EngineError("资源 ZIP 大小超出预算。")
    with archive.open("rb") as handle:
        handle.seek(max(0, size - 65557))
        tail = handle.read(65557)
        end = tail.rfind(b"PK\x05\x06")
        if end < 0 or end + 22 > len(tail):
            raise EngineError("资源 ZIP 不完整。")
        eocd_offset = size - len(tail) + end
        _, disk_no, cd_disk, count_disk, count, cd_size, cd_offset, comment = struct.unpack_from("<4s4H2LH", tail, end)
        if (
            disk_no != 0
            or cd_disk != 0
            or count_disk != count
            or not 1 <= count <= 5000
            or not 46 * count <= cd_size <= 2_000_000
            or end + 22 + comment != len(tail)
            or cd_offset + cd_size != eocd_offset
        ):
            raise EngineError("资源 ZIP 目录超出预算或布局无效（不支持分卷/ZIP64）。")
        # ZipFile honors a ZIP64 locator even when the ordinary EOCD has no
        # ZIP64 sentinel values. A maximum-length comment puts that locator
        # outside tail, so seek to its actual position before trusting this CD.
        handle.seek(eocd_offset - 20)
        if handle.read(4) == b"PK\x06\x07":
            raise EngineError("资源 ZIP 不支持 ZIP64 替代目录。")
        handle.seek(cd_offset)
        directory = handle.read(cd_size)
    offset = 0
    for _ in range(count):
        if offset + 46 > len(directory) or directory[offset : offset + 4] != b"PK\x01\x02":
            raise EngineError("资源 ZIP 目录条目无效。")
        name, extra, note, disk_no = struct.unpack_from("<4H", directory, offset + 28)
        if disk_no:
            raise EngineError("资源 ZIP 不支持分卷。")
        offset += 46 + name + extra + note
    if offset != len(directory):
        raise EngineError("资源 ZIP 目录数量或长度不匹配。")


def open_resources(archive: Path) -> Tuple[zipfile.ZipFile, bytes, List[Tuple[zipfile.ZipInfo, str, str]]]:
    check_resource_zip(archive)
    bundle = zipfile.ZipFile(archive)
    try:
        seen, total = set(), 0
        for item in bundle.infolist():
            name = item.filename[:-1] if item.is_dir() else item.filename
            safe_path(name)
            if (
                item.orig_filename != item.filename
                or not item.filename.startswith(RESOURCE_ROOT)
                or name.casefold() in seen
            ):
                raise EngineError("资源 ZIP 根目录不匹配或包含重复路径。")
            seen.add(name.casefold())
            total += item.file_size
            if item.file_size > RESOURCE_FILE_LIMIT or total > RESOURCE_TOTAL_LIMIT:
                raise EngineError("资源 ZIP 解压大小超出预算。")
            mode = stat.S_IFMT(item.external_attr >> 16)
            if item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise EngineError("资源 ZIP 含加密或不支持的压缩条目。")
            if item.filename.startswith(RESOURCE_ROOT + "resources/") or name == RESOURCE_ROOT + "resources":
                expected_mode = stat.S_IFDIR if item.is_dir() else stat.S_IFREG
                if mode not in {0, expected_mode} or (item.is_dir() and item.file_size):
                    raise EngineError("资源 ZIP 含链接、特殊文件或错误目录类型。")
            # Upstream has README symlinks outside resources. Never extract or
            # follow those (nor any other source code); only use the pinned list.
        manifest = bundle.getinfo(RESOURCE_ROOT + "resources/resources.json")
        if manifest.file_size > 1_000_000:
            raise EngineError("资源清单过大。")
        # read() without a length may inflate a forged tiny entry in a huge
        # allocation before ZipExtFile trims it to the declared file_size.
        with bundle.open(manifest) as source:
            raw = source.read(1_000_001)
        entries = [
            (bundle.getinfo(RESOURCE_ROOT + "resources/" + name), name, checksum)
            for name, checksum in resource_entries(raw)
        ]
        return bundle, raw, entries
    except KeyError:
        bundle.close()
        raise EngineError("资源 ZIP 缺少固定清单或所需素材。") from None
    except BaseException:
        bundle.close()
        raise


def extract_resource(bundle: zipfile.ZipFile, item: zipfile.ZipInfo, path: Path, checksum: str) -> int:
    secure_directory(path.parent)
    size, digest = 0, hashlib.sha256()
    with bundle.open(item) as source, path.open("xb") as output:
        while chunk := source.read(262144):
            size += len(chunk)
            if size > item.file_size or size > RESOURCE_FILE_LIMIT:
                raise EngineError("素材解压超过大小预算。")
            digest.update(chunk)
            output.write(chunk)
    if size != item.file_size or digest.hexdigest() != checksum:
        raise EngineError("素材 SHA256 或大小不匹配。")
    return size


async def install_resources(archive: Path, staging: Path, status: Status) -> None:
    progress = InstallProgress("素材解包校验", files_total=0)
    status("资源", progress)
    bundle, raw, entries = await disk(open_resources, archive, on_cancel=lambda result: result[0].close())
    try:
        progress.files_total = len(entries)
        await disk((staging / "resources.json").write_bytes, raw)
        total = 0
        for index, (item, name, checksum) in enumerate(entries, 1):
            # Yield between bounded files. Cancellation joins the current file,
            # closes the archive, then removes staging while still holding lock.
            total += await disk(extract_resource, bundle, item, staging / "resources" / name, checksum)
            progress.update(total, index)
        progress.finish()
    finally:
        await disk(bundle.close)


def extract(archive: Path, destination: Path, target: str = "linux-x86_64") -> str:
    total, seen, executable_hash = 0, set(), None
    executable = executable_name(target)
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 32:
            raise EngineError("引擎 ZIP 条目过多。")
        for item in bundle.infolist():
            name = str(safe_path(item.filename.rstrip("/") if item.is_dir() else item.filename))
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or item.flag_bits & 1:
                raise EngineError("引擎 ZIP 包含链接、特殊文件或加密条目。")
            if name.casefold() in seen:
                raise EngineError("引擎 ZIP 包含重复路径。")
            seen.add(name.casefold())
            total += item.file_size
            if total > 128_000_000:
                raise EngineError("引擎 ZIP 解压大小超限。")
            # Windows workflow may include Cargo's dependency metadata meme.d;
            # it is not executable and is not installed. Never accept extra DLLs.
            if name == "meme.d" and target == "windows-x86_64" and not item.is_dir():
                continue
            if name != executable or item.is_dir():
                raise EngineError("引擎 ZIP 布局与对应平台的根目录程序不符。")
            digest = hashlib.sha256()
            count = 0
            with bundle.open(item) as source, (destination / executable).open("xb") as output:
                while chunk := source.read(262144):
                    count += len(chunk)
                    if count > item.file_size or count > 128_000_000:
                        raise EngineError("引擎解压超过预算。")
                    digest.update(chunk)
                    output.write(chunk)
            executable_hash = digest.hexdigest()
    if executable_hash is None:
        raise EngineError("引擎 ZIP 缺少 meme 程序。")
    (destination / executable).chmod(0o700)
    return executable_hash


def validate_binary(path: Path, target: str) -> None:
    regular(path)
    with path.open("rb") as handle:
        header = handle.read(64)
        if target == "windows-x86_64":
            offset = int.from_bytes(header[60:64], "little")
            if len(header) != 64 or header[:2] != b"MZ" or not 64 <= offset <= 1_000_000:
                raise EngineError("引擎 PE 文件头不匹配。")
            handle.seek(offset)
            pe = handle.read(26)
            valid = pe[:6] == b"PE\x00\x00\x64\x86" and pe[24:26] == b"\x0b\x02"
        elif target.startswith("macos-"):
            cpu = 0x01000007 if target.endswith("x86_64") else 0x0100000C
            valid = header[:4] == b"\xcf\xfa\xed\xfe" and int.from_bytes(header[4:8], "little") == cpu
        else:
            machine = 62 if target == "linux-x86_64" else 183
            valid = header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == machine
    if len(header) < 64 or not valid:
        raise EngineError("引擎二进制格式/架构与当前环境不匹配。")
    if not os.access(path, os.X_OK):
        raise EngineError("引擎不可执行；请检查目录权限/noexec。")


def verify_install(directory: Path, target: str, progress: Optional[InstallProgress] = None) -> None:
    secure_directory(directory)
    archive = directory / "engine.zip"
    size, checksum = ASSETS[target]
    if digest_file(archive, size) != checksum:
        raise EngineError("已安装引擎包校验失败；保留数据，拒绝执行。")
    with zipfile.ZipFile(archive) as bundle:
        executable = executable_name(target)
        names = bundle.namelist()
        allowed = {executable, "meme.d"} if target == "windows-x86_64" else {executable}
        if (
            executable not in names
            or len(names) != len(set(names))
            or not set(names) <= allowed
            or sum(item.file_size for item in bundle.infolist()) > 128_000_000
        ):
            raise EngineError("已安装引擎包布局不匹配。")
        expected = hashlib.sha256(bundle.read(executable)).hexdigest()
    if digest_file(directory / executable, 128_000_000) != expected:
        raise EngineError("已安装程序校验失败；保留数据，拒绝执行。")
    raw = directory / "resources.json"
    regular(raw)
    if raw.stat().st_size > 1_000_000:
        raise EngineError("资源清单过大。")
    total = 0
    entries = resource_entries(raw.read_bytes())
    if progress is not None:
        progress.files_total = len(entries)
    for index, (name, checksum) in enumerate(entries, 1):
        path = directory / "resources" / name
        # Check every ancestor, not just the leaf, on reuse as well as install.
        if any(is_link(p) for p in (path, *path.parents)):
            raise EngineError("资源文件路径包含链接。")
        if digest_file(path, RESOURCE_FILE_LIMIT) != checksum:
            raise EngineError("已安装素材校验失败；保留数据，拒绝启动。")
        total += path.stat().st_size
        if total > RESOURCE_TOTAL_LIMIT:
            raise EngineError("资源总大小超限。")
        if progress is not None:
            progress.update(total, index)
    validate_binary(directory / executable, target)
    if progress is not None:
        progress.finish()


def remove_staging(staging: Path) -> None:
    # Existence check and removal are one joined operation, so repeated cancel
    # cannot interrupt between them and leave an abandoned partial archive.
    if staging.exists():
        shutil.rmtree(staging)


async def install(
    root: Path,
    target: str,
    status: Status,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Path:
    destination = root / f"v{VERSION}-{target}"
    if await disk(destination.exists):
        progress = InstallProgress("复核已安装程序及素材", files_total=0)
        status("校验", progress)
        await disk(verify_install, destination, target, progress)
        return destination
    # Exclusive owner lock is held by the caller. This name belongs solely to
    # this installer and can be recovered after a Runner crash.
    staging = root / f".staging-v{VERSION}-{target}"
    if await disk(is_link, staging):
        raise EngineError("暂存目录是链接，拒绝恢复。")
    try:
        await disk(remove_staging, staging)
        # Reclaim our crash residue before checking the space for a fresh
        # attempt; otherwise the residue itself can permanently block retry.
        await disk(check_space, root)
        await disk(secure_directory, staging)
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            size, checksum = ASSETS[target]
            progress = InstallProgress(f"引擎 v{VERSION}", total=size)
            status("下载", progress)
            await download(
                client,
                RELEASE_BASE + f"meme-generator-cli-{target}.zip",
                staging / "engine.zip",
                checksum,
                size,
                progress,
            )
            status("校验", "校验引擎并安全解压")
            await disk(extract, staging / "engine.zip", staging, target)
            await disk(validate_binary, staging / executable_name(target), target)
            progress = InstallProgress("上游素材归档（约 381 MB）")
            status("资源", progress)
            source = staging / "source.zip"
            await download(client, RESOURCE_ARCHIVE, source, None, RESOURCE_ARCHIVE_LIMIT, progress)
            await install_resources(source, staging, status)
            await disk(source.unlink)
            progress = InstallProgress("安装完成前复核程序及素材", files_total=0)
            status("校验", progress)
            await disk(verify_install, staging, target, progress)
            await disk((staging / "LICENSE.upstream.txt").write_bytes, UPSTREAM_LICENSE.encode("utf-8"))
        # Same filesystem, lock held; no partially installed final directory.
        await disk(os.rename, staging, destination)
        return destination
    finally:
        await disk(remove_staging, staging)
