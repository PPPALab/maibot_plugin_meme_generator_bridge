"""In-memory installation progress; reading it never starts work or a timer task."""

from time import monotonic
from typing import Callable, Optional, Tuple, Union


class InstallProgress:
    def __init__(self, label: str, total: Optional[int] = None, files_total: Optional[int] = None):
        self.label, self.total, self.files_total = label, total, files_total
        self.started = monotonic()
        # One snapshot assignment lets the disk worker publish coherent counts.
        self._sample: Tuple[int, int, Optional[float]] = (0, 0, None)

    def update(self, size: int, files: int = 0) -> None:
        self._sample = (size, files, None)

    def finish(self) -> None:
        size, files, _ = self._sample
        self._sample = (size, files, monotonic())

    def text(self) -> str:
        size, files, finished = self._sample
        elapsed = max(0.0, (monotonic() if finished is None else finished) - self.started)
        speed = size / elapsed if elapsed > 0 else 0.0
        if self.files_total is not None:
            percent = 100 * files / self.files_total if self.files_total else 0.0
            if finished is None:
                percent = min(99.9, percent)
            amount = f"{files}/{self.files_total} 个（{percent:.1f}%），已校验 {size / 1_000_000:.2f} MB"
        elif self.total:
            # Receiving the last byte is not yet successful EOF/hash validation.
            percent = 100.0 if finished is not None else min(99.9, 100 * size / self.total)
            amount = f"{size / 1_000_000:.2f}/{self.total / 1_000_000:.2f} MB（{percent:.1f}%）"
        else:
            amount = f"已下载 {size / 1_000_000:.2f} MB（总大小未知，百分比不可用）"
        seconds = int(elapsed)
        duration = f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"
        return f"{self.label} · {amount} · 均速 {speed / 1_000_000:.2f} MB/s · 耗时 {duration}"


StatusDetail = Union[str, InstallProgress]
Status = Callable[[str, StatusDetail], None]
