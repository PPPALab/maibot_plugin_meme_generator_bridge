"""Owned child lifecycle. Config/widget callbacks only enqueue work."""

from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

import httpx

from .engine_guard import GUARD
from .engine_install import (
    EngineError,
    detect_target,
    disk,
    executable_name,
    install,
    is_link,
    join_task,
    secure_directory,
)
from .engine_progress import InstallProgress, StatusDetail
from .engine_release import VERSION

logger = logging.getLogger(__name__)

# Executed by the existing Python interpreter in isolated stdlib mode, NOT a
# downloaded script. Avoid preexec_fn in a multithreaded Runner. The lock FD is
# inherited by exec, so even an unkillable orphan blocks another installation.
CHILD_BOOTSTRAP = """
import ctypes, os, signal, sys
parent = int(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
    raise SystemExit(120)
if os.getppid() != parent:
    raise SystemExit(121)
os.execv(sys.argv[2], sys.argv[2:])
"""


def acquire(root: Path) -> int:
    if os.name == "nt":
        from .engine_windows import private_root

        secure_directory(root.parent)
        if is_link(root):
            raise EngineError("托管目录不得为链接/重解析点。")
        private_root(root)
    secure_directory(root)
    info = root.stat()
    if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise EngineError("托管目录所有者或写权限不安全；须为当前用户所有且不可被组/其他用户写入。")
    if is_link(root / "owner.lock"):
        raise EngineError("托管锁文件不得为链接/重解析点。")
    if os.name == "nt":
        from .engine_windows import exclusive_file

        return exclusive_file(root / "owner.lock")
    import fcntl

    fd = os.open(root / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise EngineError("托管锁文件不是独立普通文件。")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EngineError("已有托管实例或尚未退出的旧进程持有锁；未接管或终止它，请等待退出后重试。") from None
        return fd
    except BaseException:
        os.close(fd)
        raise


def prepare_home(home: Path, port: int) -> None:
    secure_directory(home)
    secure_directory(home / "tmp")
    # No custom fonts path exists at runtime in v0.2.3; MEME_HOME must contain
    # the verified resources. External libraries stay explicitly disabled.
    config = (
        "[meme]\nload_external_memes = false\n"
        "[font]\nuse_local_fonts = false\n"
        "[resource]\ndownload_fonts = false\n"
        '[server]\nhost = "127.0.0.1"\n'
        f"port = {port}\n"
    )
    fd, name = tempfile.mkstemp(prefix=".config-", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(config)
        os.replace(name, home / "config.toml")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise EngineError("托管端口已占用或不可绑定；未接管、未终止占用者。") from None


def owns_listener(pid: int, port: int) -> bool:
    # Checking version alone could accept someone else's identical engine after
    # a bind race. Require the listening socket to belong to OUR exact child.
    if sys.platform == "win32":
        from .engine_windows import owns_listener as windows_owns_listener

        return windows_owns_listener(pid, port)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"],
                capture_output=True,
                timeout=1,
                check=False,
            )
            return macos_listener_output(result.stdout, pid, port) if result.returncode == 0 else False
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        inodes = set()
        for line in Path(f"/proc/{pid}/net/tcp").read_text().splitlines()[1:]:
            parts = line.split()
            if parts[1] == f"0100007F:{port:04X}" and parts[3] == "0A":
                inodes.add("socket:[" + parts[9] + "]")
        return any(os.readlink(fd) in inodes for fd in Path(f"/proc/{pid}/fd").iterdir())
    except (OSError, IndexError):
        return False


def macos_listener_output(data: bytes, pid: int, port: int) -> bool:
    lines = data.splitlines()
    return f"p{pid}".encode() in lines and f"n127.0.0.1:{port}".encode() in lines


async def health(url: str, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    async with httpx.AsyncClient(timeout=1, trust_env=False, follow_redirects=False, transport=transport) as client:
        values = []
        for route, limit in (("/meme/version", 100), ("/meme/infos", 4_000_000)):
            async with client.stream("GET", url + route, headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    if len(raw) + len(chunk) > limit:
                        raise EngineError("托管健康响应超过预算。")
                    raw.extend(chunk)
                values.append(bytes(raw))
        if values[0].decode().strip() != VERSION:
            raise EngineError("托管引擎版本不匹配。")
        infos = json.loads(values[1])
        if (
            not isinstance(infos, list)
            or not infos
            or not all(isinstance(x, dict) and isinstance(x.get("key"), str) for x in infos)
        ):
            raise EngineError("托管引擎元数据接口不兼容。")


def remove_data(root: Path) -> None:
    """Keep the lock inode stable so another Runner cannot bypass exclusion."""
    if not root.exists() and not is_link(root):
        return
    fd = acquire(root)
    try:
        for path in root.iterdir():
            if path.name == "owner.lock":
                continue
            if is_link(path):
                raise EngineError("托管数据含链接/重解析点，未继续删除，请管理员核实。")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    finally:
        os.close(fd)


class EngineManager:
    def __init__(self, data_dir: Path):
        # Construction and status reads do not touch disk/network/processes.
        self.root = Path(data_dir).absolute() / "managed-engine"
        self.phase = "关闭"
        self._detail: StatusDetail = "使用外部 base_url；未托管引擎"
        self.desired: Optional[int] = None
        self._url: Optional[str] = None
        self._wake = asyncio.Event()
        self._supervisor: Optional[asyncio.Task[None]] = None
        self._worker: Optional[asyncio.Task[None]] = None
        self._closing = False
        self.process: Optional[asyncio.subprocess.Process] = None
        self._drains: List[asyncio.Task[None]] = []
        self._engine_pid: Optional[int] = None
        self._target: Optional[str] = None
        self._guarded = sys.platform in {"darwin", "win32"}
        self._job = None
        self.logs = deque(maxlen=16)  # 16 * 4096 bytes, private memory only
        self.retry_after = 0.0
        self._remove_pending = False

    def remove(self) -> None:
        if self._closing or self.desired is not None:
            raise EngineError("请先关闭托管开关并保存，再删除托管数据。")
        if self._remove_pending or self.phase == "清理":
            return
        self._remove_pending = True
        self.status("清理", "后台停止受管任务并删除程序、安装包和素材")
        self._enqueue()

    def status(self, phase: str, detail: StatusDetail) -> None:
        self.phase, self._detail = phase, detail

    @property
    def detail(self) -> str:
        # Elapsed time stays live even during a network stall. Widget refresh
        # only formats this memory snapshot; it cannot start installation work.
        return self._detail.text() if isinstance(self._detail, InstallProgress) else self._detail

    @property
    def url(self) -> Optional[str]:
        if self.desired is not None and self.process is not None and self.process.returncode is None:
            return self._url
        return None

    def configure(self, enabled: bool, port: int) -> None:
        desired = port if enabled else None
        if self._closing or desired == self.desired:
            return
        if enabled:
            # A new enable request supersedes deletion that has not started.
            # If deletion is already on disk, the controller joins it first.
            self._remove_pending = False
        self.desired, self._url = desired, None
        self.status("检测" if enabled else "停止", "后台处理环境检测/安装/进程切换")
        self._enqueue()

    def _enqueue(self) -> None:
        self._wake.set()
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._control())

    def retry(self) -> None:
        if self._closing or self.desired is None or self.phase != "失败" or time.monotonic() < self.retry_after:
            return
        self.status("检测", "后台重试；不会解除重任务熔断")
        self._enqueue()

    async def _control(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._worker is not None:
                self._worker.cancel()
                results = await asyncio.gather(self._worker, return_exceptions=True)
                self._worker = None
                if any(isinstance(result, Exception) for result in results):
                    self.retry_after = time.monotonic() + 30
                    self.status("失败", "受管任务清理失败；拒绝启动新实例，请核实旧进程与磁盘状态。")
                    return
            if self.process is not None:
                try:
                    await self._stop()
                except Exception:
                    self.retry_after = time.monotonic() + 30
                    self.status("失败", "旧受管进程仍未能回收；未启动新实例，请核实运行环境。")
                    return
            # Changes arriving while cleanup awaited are already reflected in
            # desired. Do not restart the newly launched worker for stale wakes.
            self._wake.clear()
            self._url = None
            if self._closing:
                return
            if self._remove_pending:
                self._remove_pending = False
                try:
                    await disk(remove_data, self.root)
                except EngineError as exc:
                    self.status("失败", str(exc))
                except Exception as exc:
                    logger.warning("托管数据删除失败 type=%s", type(exc).__name__)
                    self.status("失败", f"{type(exc).__name__}：托管数据删除失败；请检查占用和权限后重试删除。")
                else:
                    self.status("关闭", "托管数据已删除；下次开启将重新下载")
                # Configuration changes during disk work are handled next.
                continue
            if self.desired is None:
                self.status("关闭", "使用外部 base_url；下载数据已保留")
            else:
                self._worker = asyncio.create_task(self._run(self.desired))

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            self.logs.append(chunk)

    async def _start(self, home: Path, port: int, fd: int) -> None:
        await disk(prepare_home, home, port)
        await disk(check_port, port)
        env = {
            "MEME_HOME": str(home),
            "HOME": str(home),
            "TMPDIR": str(home / "tmp"),
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        executable = executable_name(self._target or detect_target())
        command = [str(home / executable), "run", "--host", "127.0.0.1", "--port", str(port)]
        options: Dict[str, Any] = {"pass_fds": (fd,), "start_new_session": True}
        inherited_handle = None
        if os.name == "nt":
            import msvcrt
            from .engine_windows import Job

            # Only OS bootstrap variables, never proxy/token/PYTHON/LD_* values.
            for key in ("SystemRoot", "WINDIR"):
                if key in os.environ:
                    env[key] = os.environ[key]
            env.update(TEMP=str(home / "tmp"), TMP=str(home / "tmp"), USERPROFILE=str(home))
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            self._job = Job()
            inherited_handle = msvcrt.get_osfhandle(fd)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [inherited_handle]}
            options = {"startupinfo": startup, "close_fds": True, "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            os.set_handle_inheritable(inherited_handle, True)
        if self._guarded:
            bootstrap = [GUARD, str(inherited_handle if inherited_handle is not None else fd)]
            options["stdin"] = asyncio.subprocess.PIPE
        else:
            bootstrap = [CHILD_BOOTSTRAP, str(os.getpid())]
        # Join spawn even on cancellation, otherwise a child can appear after
        # cleanup has decided there is no process to terminate.
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                *bootstrap,
                *command,
                cwd=home,
                env=env,
                **options,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        try:
            self.process, cancelled = await join_task(spawn)
            if cancelled:
                raise asyncio.CancelledError
        finally:
            if inherited_handle is not None:
                os.set_handle_inheritable(inherited_handle, False)
            if self.process is not None:
                # stderr is drained even if job assignment or the PID handshake fails.
                self._drains = [asyncio.create_task(self._drain(self.process.stderr))]
                if not self._guarded:
                    self._drains.append(asyncio.create_task(self._drain(self.process.stdout)))
        if self._guarded:
            try:
                if self._job is not None:
                    self._job.assign(self.process.pid)
                self.process.stdin.write(b"start\n")
                await self.process.stdin.drain()
                line = await asyncio.wait_for(self.process.stdout.readline(), 3)
                info = json.loads(line) if len(line) <= 100 else {}
                if type(info.get("pid")) is not int or info["pid"] <= 0:
                    raise EngineError("受管辅助进程未提供有效的子进程归属。")
                self._engine_pid = info["pid"]
            finally:
                self._drains.append(asyncio.create_task(self._drain(self.process.stdout)))
        else:
            self._engine_pid = self.process.pid

    async def _stop(self) -> None:
        proc = self.process
        if proc is not None:
            if proc.returncode is None:
                if self._guarded and proc.stdin is not None:
                    # EOF requests graceful child shutdown and also works when
                    # the Runner disappears. The helper bounds its own cleanup.
                    proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.wait(), 6)
                    except asyncio.TimeoutError:
                        if self._job is not None:
                            self._job.close()
                        elif proc.returncode is None:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        await asyncio.wait_for(proc.wait(), 2)
                else:
                    await self._stop_direct(proc)
            self.process = None
        if self._job is not None:
            self._job.close()
            self._job = None
        self._engine_pid = None

    async def _stop_direct(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(proc.wait(), 2)

    async def _run(self, port: int) -> None:
        fd = None
        try:
            self.status("检测", "检查当前进程所在 OS/CPU/运行库、数据目录与端口；非已测环境为实验性支持")
            target = await disk(detect_target)
            self._target = target
            fd = await disk(acquire, self.root, on_cancel=os.close)
            await disk(check_port, port)
            home = await asyncio.wait_for(install(self.root, target, self.status), 1800)
            self.status("启动", "启动本地引擎并核验进程归属、版本与 HTTP 接口")
            await self._start(home, port, fd)
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while True:
                if self.process.returncode is not None:
                    raise EngineError("引擎启动退出；请检查架构、运行库、noexec 和端口；未自动安装系统依赖。")
                if time.monotonic() >= deadline:
                    raise EngineError(
                        "引擎启动超时或系统不允许核验端口归属（Linux/Android /proc、macOS lsof、Windows TCP API）。"
                    )
                if await disk(owns_listener, self._engine_pid, port):
                    try:
                        await health(url)
                        if self.process.returncode is None and await disk(owns_listener, self._engine_pid, port):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                await asyncio.sleep(0.2)
            if self.desired == port and not self._closing:
                self._url = url
                self.status("就绪", f"v{VERSION} · {target} · 仅回环 {port}")
            await self.process.wait()
            raise EngineError("受管引擎已退出；30 秒后可手动重试，不自动重启。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.retry_after = time.monotonic() + 30
            detail = (
                str(exc)
                if isinstance(exc, EngineError)
                else f"{type(exc).__name__}：安装/启动失败；检查网络、磁盘、权限或运行库。"
            )
            self.status("失败", f"{self._target or '未匹配平台'} · {detail}")
        finally:
            self._url = None
            try:
                await self._stop()
            finally:
                for task in self._drains:
                    task.cancel()
                await asyncio.gather(*self._drains, return_exceptions=True)
                self._drains = []
                if fd is not None:
                    # Do not LOCK_UN: the child's inherited description must keep
                    # the lock if it could not exit. Never kill an unowned PID.
                    await disk(os.close, fd)

    async def close(self) -> None:
        self._closing, self.desired, self._url = True, None, None
        if self._supervisor is not None:
            self._wake.set()
            await self._supervisor
        if self.process is not None:
            raise EngineError("受管进程未能按时回收；锁仍保护所有权，请管理员核实。")
