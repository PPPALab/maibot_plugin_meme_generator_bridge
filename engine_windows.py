"""Windows native ownership primitives. Imported safely on other platforms."""

from pathlib import Path

import ctypes
import os
import socket
import struct

from .engine_install import EngineError


def private_acl_matches(value: str) -> bool:
    # Windows may add the auto-inherited/requested bookkeeping bits even for
    # an explicitly protected DACL. Accept those, not any additional trustee.
    flags, separator, ace = value.partition("(")
    if not separator or not flags.startswith("D:") or "P" not in flags[2:]:
        return False
    remainder = flags[2:]
    for flag in ("AR", "AI", "P"):
        remainder = remainder.replace(flag, "", 1)
    return not remainder and ace in {"A;OICI;FA;;;OW)", "A;OICI;0x1f01ff;;;OW)"}


def private_root(path: Path) -> None:
    """Create an owner-only inheritable DACL; never rewrite an existing ACL."""
    from ctypes import wintypes as w

    api = kernel()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    expected = "D:P(A;OICI;FA;;;OW)"

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]

    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = w.BOOL
    api.LocalFree.argtypes, api.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    api.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    api.CreateDirectoryW.restype = w.BOOL
    if not convert(expected, 1, ctypes.byref(descriptor), None):
        raise EngineError("Windows 无法构造私有托管目录权限。")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not api.CreateDirectoryW(str(path), ctypes.byref(attributes)) and ctypes.get_last_error() != 183:
            raise EngineError("Windows 无法创建私有托管目录。")
    finally:
        api.LocalFree(descriptor)

    read = advapi.GetFileSecurityW
    read.argtypes = [w.LPCWSTR, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    read.restype = w.BOOL
    size = w.DWORD()
    read(str(path), 4, None, 0, ctypes.byref(size))
    if not 1 <= size.value <= 65536:
        raise EngineError("无法核验 Windows 托管目录权限。")
    buffer = ctypes.create_string_buffer(size.value)
    if not read(str(path), 4, buffer, size.value, ctypes.byref(size)):
        raise EngineError("无法读取 Windows 托管目录权限。")
    text = w.LPWSTR()
    stringify = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    stringify.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.c_void_p]
    stringify.restype = w.BOOL
    if not stringify(buffer, 1, 4, ctypes.byref(text), None):
        raise EngineError("无法核验 Windows 托管目录 DACL。")
    try:
        if not private_acl_matches(text.value or ""):
            raise EngineError("Windows 既有托管目录不是预期的私有权限；未覆写权限，请管理员核实。")
    finally:
        api.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def kernel():
    from ctypes import wintypes as w

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": (w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]),
        "CloseHandle": (w.BOOL, [w.HANDLE]),
        "CreateJobObjectW": (w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
        "SetInformationJobObject": (w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
        "AssignProcessToJobObject": (w.BOOL, [w.HANDLE, w.HANDLE]),
        "OpenProcess": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    return api


def exclusive_file(path: Path) -> int:
    from ctypes import wintypes as w

    import msvcrt

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", w.DWORD),
            ("created", w.FILETIME),
            ("accessed", w.FILETIME),
            ("written", w.FILETIME),
            ("volume", w.DWORD),
            ("high", w.DWORD),
            ("low", w.DWORD),
            ("links", w.DWORD),
            ("index_high", w.DWORD),
            ("index_low", w.DWORD),
        ]

    api = kernel()
    # Share mode ZERO: copies of this handle inherited by our helper/engine
    # retain exclusion even if the Runner dies. Do not use PID-file ownership.
    handle = api.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise EngineError("托管锁被占用或 Windows 数据目录不可写；未接管旧进程。")
    try:
        info = FileInfo()
        function = api.GetFileInformationByHandle
        function.argtypes, function.restype = [w.HANDLE, ctypes.POINTER(FileInfo)], w.BOOL
        if not function(handle, ctypes.byref(info)) or info.attributes & 0x400 or info.links != 1:
            raise EngineError("Windows 托管锁必须是独立普通文件，不得为重解析点或硬链接。")
        return msvcrt.open_osfhandle(handle, os.O_RDWR)
    except BaseException:
        api.CloseHandle(handle)
        raise


def native_machine() -> str:
    # Native OS architecture, not the architecture of an emulated x64 Python.
    from ctypes import wintypes as w

    api = kernel()
    # Modern ARM64 emulation can virtualize GetNativeSystemInfo. Prefer the
    # explicit native machine code; older Windows without this API predates it.
    query = getattr(api, "IsWow64Process2", None)
    if query is not None:
        query.argtypes = [w.HANDLE, ctypes.POINTER(w.USHORT), ctypes.POINTER(w.USHORT)]
        query.restype = w.BOOL
        current = api.GetCurrentProcess
        current.argtypes, current.restype = [], w.HANDLE
        process_machine, native = w.USHORT(), w.USHORT()
        if not query(current(), ctypes.byref(process_machine), ctypes.byref(native)):
            raise EngineError("Windows 无法确认原生 CPU 架构；未按模拟架构下载。")
        return {0x8664: "x86_64", 0xAA64: "aarch64", 0x014C: "x86"}.get(native.value, "unknown")
    function = api.GetNativeSystemInfo
    function.argtypes, function.restype = [ctypes.c_void_p], None
    info = ctypes.create_string_buffer(64)
    function(info)
    architecture = struct.unpack_from("<H", info.raw)[0]
    return {9: "x86_64", 12: "aarch64", 0: "x86"}.get(architecture, "unknown")


class Job:
    """Assign a gated helper BEFORE it may launch the engine; fail closed."""

    def __init__(self):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", w.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_processes", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self.api = kernel()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise EngineError("Windows 无法创建受管 Job Object。")
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise EngineError("Windows 无法设置退出回收 Job Object。")

    def assign(self, pid: int) -> None:
        process = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise EngineError("Windows 无法取得自己创建的辅助进程句柄。")
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise EngineError("Windows 宿主 Job/权限限制阻止受管进程归属；引擎尚未启动。")
        finally:
            self.api.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def table_owns_listener(data: bytes, pid: int, port: int) -> bool:
    if len(data) < 4:
        return False
    count = struct.unpack_from("<I", data)[0]
    if count > (len(data) - 4) // 24:
        return False
    for offset in range(4, 4 + count * 24, 24):
        state, address, local_port, _, _, owner = struct.unpack_from("<6I", data, offset)
        if (
            state == 2
            and owner == pid
            and socket.ntohs(local_port & 0xFFFF) == port
            and socket.inet_ntoa(struct.pack("<I", address)) == "127.0.0.1"
        ):
            return True
    return False


def owns_listener(pid: int, port: int) -> bool:
    from ctypes import wintypes as w

    api = ctypes.WinDLL("iphlpapi", use_last_error=True)
    function = api.GetExtendedTcpTable
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG]
    function.restype = w.DWORD
    size = w.DWORD()
    result = function(None, ctypes.byref(size), False, 2, 3, 0)
    if result not in {0, 122} or not 4 <= size.value <= 4_000_000:
        return False
    buffer = ctypes.create_string_buffer(size.value)
    if function(buffer, ctypes.byref(size), False, 2, 3, 0) != 0:
        return False
    return table_owns_listener(buffer.raw[: size.value], pid, port)
