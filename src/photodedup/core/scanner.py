"""目录扫描：递归遍历、格式白名单、跳过规则。

只做文件发现，不解码；目的是先把「要看哪些文件」定下来，
并在这个过程中把无权限、损坏、临时文件排除干净。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# 常见图片格式（HEIC/HEIF 依赖 pillow-heif，没装会在指纹阶段报错并跳过）
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".dib",
    ".tif", ".tiff", ".gif", ".heic", ".heif", ".avif", ".jxl", ".ico",
}

# 这些目录不进去：系统/同步/临时/我们的暂存区
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "@eadir", ".thumbnails",
    ".thumbdata", "__macosx", "node_modules", ".git", ".svn", ".hg",
    ".venv", "venv", "env", "build", "dist", "__pycache__", "lost+found",
    ".trash", ".trashes", "#recycle", ".dropbox.cache", "photodedup_cache",
    ".idea", ".vscode",
}

# 这些文件名直接跳过
SKIP_FILES = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db"}

# 我们自己的待删暂存目录名前缀
STAGING_PREFIX = "_去重待删"


@dataclass(slots=True)
class FileEntry:
    path: Path
    size: int
    mtime: float


ProgressFn = Callable[[int, str], None]
CancelFn = Callable[[], bool]


def _skip_dir_name(name: str) -> bool:
    low = name.lower()
    if low in SKIP_DIRS:
        return True
    return low.startswith(STAGING_PREFIX)


def scan_directory(root: Path | str, recursive: bool = True,
                   skip_hidden: bool = True,
                   progress: ProgressFn | None = None,
                   cancel: CancelFn | None = None) -> tuple[list[FileEntry], list[str]]:
    """扫描目录，返回 (图片文件列表, 警告信息列表)。

    遇到无权限目录不中断，记一条警告继续走。
    """
    root = Path(root)
    found: list[FileEntry] = []
    warnings: list[str] = []
    stack: list[Path] = [root]
    seen_dirs = 0

    while stack:
        if cancel and cancel():
            break
        cur = stack.pop()
        seen_dirs += 1
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if skip_hidden and name.startswith("."):
                                continue
                            if _skip_dir_name(name):
                                continue
                            if recursive:
                                stack.append(Path(entry.path))
                            continue

                        name = entry.name
                        if name.lower() in SKIP_FILES:
                            continue
                        if name.startswith("._") or name.startswith("~$"):
                            continue
                        if skip_hidden and name.startswith("."):
                            continue
                        ext = os.path.splitext(name)[1].lower()
                        if ext not in IMAGE_EXTS:
                            continue
                        st = entry.stat()
                        if st.st_size <= 0:
                            continue
                        found.append(FileEntry(Path(entry.path), st.st_size, st.st_mtime))
                    except OSError:
                        continue
        except PermissionError:
            warnings.append(f"无权限跳过：{cur}")
        except OSError as e:
            warnings.append(f"读取失败 {cur}：{e}")
        if progress and seen_dirs % 50 == 0:
            progress(len(found), str(cur))

    found.sort(key=lambda f: str(f.path).lower())
    return found, warnings


def group_by_size(files: Iterable[FileEntry]) -> dict[int, list[int]]:
    """按文件大小分桶 —— 字节相同的文件必然大小相同，可以先砍掉大部分。"""
    buckets: dict[int, list[int]] = {}
    for i, f in enumerate(files):
        buckets.setdefault(f.size, []).append(i)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"
