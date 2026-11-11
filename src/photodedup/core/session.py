"""会话：擂台队列、删除、撤销、报告。

对决模型（不预生成队列，全部由状态推导，避免队列与实际一致性出错）：
- 每组有 alive（还活着的成员）、kept（用户说「都留」的成员）、champion（擂主）
- 挑战者 = 按质量排序后，第一个既活着、不是擂主、也没被「都留」的成员
- 删掉擂主 → 挑战者顶上来当擂主；删掉挑战者 → 擂主继续
- 都留 → 挑战者退出擂台但文件保留
- 活着只剩一张 → 该组结束

安全规则（不可绕过）：
1. 一组至少留一张
2. 删除前校验文件大小/修改时间没变，变了就拒删
3. 删除失败不静默：抛 DeleteError 给界面提示，状态不动
4. 默认「系统回收站」模式（跟着 Windows 习惯走），撤销时程序会自己去回收站捞回来；
   捞不回来就明确报错，让用户手动还原，绝不假装成功
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import APP_NAME, __version__
from .models import ImageInfo, PairVerdict, REL_LABEL
from .pipeline import ScanOptions, ScanResult, verify_missing_pair
from .recommend import Recommendation, recommend
from .scanner import STAGING_PREFIX, human_size

MODE_STAGING = "staging"
MODE_TRASH = "trash"


class DeleteError(RuntimeError):
    """删除/撤销失败，带上人能看懂的原因。"""


@dataclass
class Bout:
    """一次对决：擂主 vs 挑战者。"""

    group_id: int
    group_rel: str
    champion: int
    challenger: int
    group_total: int
    group_alive: int
    round_no: int


@dataclass
class DeleteRecord:
    path: Path
    size: int
    mtime: float
    staged: Path | None
    mode: str
    group_id: int = 0
    when: float = field(default_factory=time.time)


class _GroupState:
    __slots__ = ("ranked", "alive", "kept", "champion", "done", "round_no")

    def __init__(self, ranked: list[int]) -> None:
        self.ranked = ranked
        self.alive = list(ranked)
        self.kept: set[int] = set()
        self.champion = ranked[0]
        self.done = False
        self.round_no = 0


class Session:
    """一次「浏览并清理」的全过程。"""

    def __init__(self, result: ScanResult, options: ScanOptions,
                 mode: str = MODE_TRASH, staging_root: Path | None = None) -> None:
        self.result = result
        self.opt = options
        self.mode = mode
        self.root = Path(result.root)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.staging_root = Path(staging_root) if staging_root else (
            self.root / f"{STAGING_PREFIX}_{stamp}")

        self.deleted: list[DeleteRecord] = []
        self.kept_both: list[tuple[int, int]] = []
        self._decisions: list[dict] = []
        self._processed = 0
        self._planned = sum(max(0, g.size - 1) for g in result.groups)

        self.groups: dict[int, _GroupState] = {}
        for gid, g in enumerate(result.groups):
            ranked = sorted(g.members, key=lambda m: self._rank_key(self.result.files[m]))
            self.groups[gid] = _GroupState(ranked)

    # ---------------- 排序与队列 ----------------
    @staticmethod
    def _rank_key(info: ImageInfo) -> tuple:
        """组内初始排序：元数据 -> 像素 -> 压缩质量 -> 体积 -> 文件名。

        特意不用锐度排第一：加水印或后期锐化会把拉普拉斯方差抬上去，
        容易让「加工过的版本」抢到擂主位。真正的优劣由逐对 recommend 决定，
        这里只保证起始点合理。
        """
        from .recommend import _copy_marker

        return (-info.exif_score, -info.pixels, -(info.jpeg_quality or 0),
                -info.size, 1 if _copy_marker(info) else 0,
                str(info.path).lower())

    def _challenger_of(self, st: _GroupState) -> int | None:
        for m in st.ranked:
            if m in st.alive and m != st.champion and m not in st.kept:
                return m
        return None

    def current(self) -> Bout | None:
        for gid in sorted(self.groups):
            st = self.groups[gid]
            if st.done:
                continue
            ch = self._challenger_of(st)
            if ch is None or len(st.alive) < 2:
                st.done = True
                continue
            ch_idx = st.ranked.index(ch)
            return Bout(group_id=gid, group_rel=self.result.groups[gid].rel,
                        champion=st.champion, challenger=ch,
                        group_total=len(st.ranked), group_alive=len(st.alive),
                        round_no=ch_idx)
        return None

    def progress(self) -> tuple[int, int]:
        return min(self._processed, self._planned), self._planned

    def groups_done(self) -> int:
        return sum(1 for st in self.groups.values() if st.done)

    # ---------------- 判定与推荐 ----------------
    def pair_verdict(self) -> PairVerdict | None:
        bout = self.current()
        if not bout:
            return None
        a, b = bout.champion, bout.challenger
        key = (min(a, b), max(a, b))
        pv = self.result.edges.get(key)
        if pv is None:
            pv = verify_missing_pair(self.result, a, b, self.opt)
        return pv

    def advice(self) -> Recommendation | None:
        bout = self.current()
        if not bout:
            return None
        pv = self.pair_verdict()
        a, b = self.result.files[bout.champion], self.result.files[bout.challenger]
        return recommend(a, b, pv)

    # ---------------- 操作 ----------------
    def _check_intact(self, info: ImageInfo) -> None:
        p = info.path
        if not p.exists():
            raise DeleteError(f"文件已不存在：{p}")
        st = p.stat()
        if int(st.st_size) != int(info.size):
            raise DeleteError(
                f"文件大小已变化（{info.size} → {st.st_size}），可能被外部改动，已跳过：{p.name}")
        if abs(st.st_mtime - info.mtime) > 2:
            raise DeleteError(f"文件修改时间已变化，已跳过：{p.name}")

    def delete(self, which: int) -> DeleteRecord:
        """删除当前对决中的一张（which 是全局图片索引）。"""
        bout = self.current()
        if not bout:
            raise DeleteError("已经没有待处理的对比了")
        if which not in (bout.champion, bout.challenger):
            raise DeleteError("指定的文件不在当前对决里")

        st = self.groups[bout.group_id]
        if len(st.alive) <= 1:
            raise DeleteError("这一组只剩一张了，不能继续删")

        info = self.result.files[which]
        self._check_intact(info)
        rec = self._move_out(info, bout.group_id)
        self.deleted.append(rec)
        self._decisions.append({"action": "delete", "path": str(info.path),
                                "group": bout.group_id, "mode": self.mode})
        self._processed += 1

        st.alive.remove(which)
        if which == st.champion:
            st.champion = st.alive[0]
        if len(st.alive) <= 1 or self._challenger_of(st) is None:
            st.done = True
        return rec

    def keep_both(self) -> None:
        """两张都留：本对不再问，挑战者退出擂台但文件保留。"""
        bout = self.current()
        if not bout:
            return
        st = self.groups[bout.group_id]
        st.kept.add(bout.challenger)
        self.kept_both.append((bout.champion, bout.challenger))
        self._decisions.append({"action": "keep_both",
                                "a": str(self.result.files[bout.champion].path),
                                "b": str(self.result.files[bout.challenger].path)})
        self._processed += 1
        if self._challenger_of(st) is None:
            st.done = True

    def skip_group(self) -> None:
        """整组跳过，不再询问。"""
        bout = self.current()
        if not bout:
            return
        self.groups[bout.group_id].done = True
        self._decisions.append({"action": "skip_group", "group": bout.group_id})

    def mark_group_kept(self, gid: int) -> None:
        self.groups[gid].done = True

    # ---------------- 删除实现 ----------------
    def _move_out(self, info: ImageInfo, gid: int) -> DeleteRecord:
        staging: Path | None = None
        if self.mode == MODE_STAGING:
            try:
                rel = info.path.relative_to(self.root)
            except ValueError:
                rel = Path(info.path.name)
            target = self.staging_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(info.path), str(target))
            except Exception as e:
                raise DeleteError(f"移动到暂存区失败：{e}") from e
            staging = target
        else:
            try:
                from send2trash import send2trash
                send2trash(str(info.path))
            except Exception as e:
                raise DeleteError(
                    f"移入回收站失败：{e}\n"
                    "网络盘、U 盘或回收站被关闭的分区上没有回收站。"
                    "可以回开始页把删除方式换成「移到待删文件夹」，那样任何位置都能删且可撤销。"
                ) from e
        return DeleteRecord(path=info.path, size=info.size, mtime=info.mtime,
                            staged=staging, mode=self.mode, group_id=gid)

    def undo(self) -> DeleteRecord | None:
        """撤销最近一次删除。失败抛 DeleteError。"""
        if not self.deleted:
            return None
        rec = self.deleted[-1]
        if rec.mode == MODE_STAGING and rec.staged:
            if not rec.staged.exists():
                raise DeleteError(f"暂存区里找不到文件了：{rec.staged}")
            if rec.path.exists():
                raise DeleteError(f"原位置已有同名文件，未覆盖：{rec.path}")
            rec.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(rec.staged), str(rec.path))
            except Exception as e:
                raise DeleteError(f"还原失败：{e}") from e
        else:
            if not restore_from_trash(rec.path):
                raise DeleteError(
                    "自动从回收站还原没成功（可能已被清空，或回收站里同名的不是这一份），"
                    "请打开回收站右键「还原」")

        self.deleted.pop()
        self._decisions.append({"action": "undo", "path": str(rec.path)})
        # 状态回滚：重新标记为活着，并让它回到擂台
        st = self.groups.get(rec.group_id)
        if st is not None:
            if rec.path.exists() and self.result.files:
                pass
            idx = None
            for i, info in enumerate(self.result.files):
                if info.path == rec.path:
                    idx = i
                    break
            if idx is not None:
                if idx not in st.alive:
                    st.alive.append(idx)
                    st.alive.sort(key=lambda m: st.ranked.index(m) if m in st.ranked else 999)
                st.done = False
        self._processed = max(0, self._processed - 1)
        return rec

    # ---------------- 收尾 ----------------
    def cleanup_staging(self) -> int:
        """彻底清空暂存区，返回释放的字节数。"""
        if not self.staging_root.exists():
            return 0
        total = 0
        for p in self.staging_root.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(self.staging_root, ignore_errors=True)
        return total

    def save_log(self, path: Path) -> None:
        data = {
            "app": APP_NAME, "version": __version__,
            "root": str(self.root), "mode": self.mode,
            "staging": str(self.staging_root),
            "when": datetime.now().isoformat(timespec="seconds"),
            "deleted": [{"path": str(r.path), "size": r.size,
                         "staged": str(r.staged) if r.staged else None,
                         "mode": r.mode, "when": r.when} for r in self.deleted],
            "kept_both": [[str(self.result.files[a].path),
                           str(self.result.files[b].path)] for a, b in self.kept_both],
            "decisions": self._decisions,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_report(self, out_dir: Path | None = None) -> tuple[Path, Path]:
        out_dir = Path(out_dir or self.root)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        md_path = out_dir / f"去重报告-{stamp}.md"
        csv_path = out_dir / f"去重报告-{stamp}.csv"

        st = self.result.stats
        freed = sum(r.size for r in self.deleted)
        lines = [
            f"# {APP_NAME} · 去重报告",
            "",
            f"- 扫描目录：`{self.root}`",
            f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"- 扫描耗时：{st.elapsed:.1f} 秒",
            f"- 图片总数：{st.images}（跳过或无法读取 {st.skipped}）",
            f"- 重复组：{st.groups} 组，重复副本 {st.duplicated} 个，"
            f"理论可省 {human_size(st.wasted_bytes)}",
            f"- 本次处理：删除 **{len(self.deleted)}** 个文件，释放 **{human_size(freed)}**",
            f"- 删除方式：{'移动到暂存区（可还原）' if self.mode == MODE_STAGING else '系统回收站'}",
        ]
        if self.mode == MODE_STAGING:
            lines.append(f"- 暂存区：`{self.staging_root}`")
        lines += ["", "## 已删除的文件", "",
                  "| # | 文件 | 大小 | 方式 |", "|---|---|---|---|"]
        for i, r in enumerate(self.deleted, 1):
            lines.append(f"| {i} | `{r.path}` | {human_size(r.size)} | "
                         f"{'暂存区' if r.mode == MODE_STAGING else '回收站'} |")
        if not self.deleted:
            lines.append("| - | 本次没有删除任何文件 | - | - |")

        lines += ["", "## 选择「都保留」的对", ""]
        for a, b in self.kept_both:
            lines.append(f"- `{self.result.files[a].path}` ↔ "
                         f"`{self.result.files[b].path}`")
        if not self.kept_both:
            lines.append("- 无")

        lines += ["", "## 未处理完的组", ""]
        pending = [gid for gid, s in self.groups.items() if not s.done]
        for gid in pending:
            g = self.result.groups[gid]
            names = "、".join(f"`{self.result.files[m].name}`" for m in g.members[:6])
            lines.append(f"- 组 {gid + 1}（{g.size} 张，{REL_LABEL.get(g.rel, g.rel)}）：{names}")
        if not pending:
            lines.append("- 无，全部处理完毕")

        lines += ["", "## 低置信候选（被算法排除，未纳入审阅）", "",
                  "这些对有几何证据但没通过阈值（疑似连拍/局部重叠），"
                  "如确信是重复请手动处理。", ""]
        low = getattr(self.result, "low_confidence_groups", [])
        for i, g in enumerate(low, 1):
            names = "、".join(f"`{self.result.files[m].name}`" for m in g.members[:6])
            more = f" 等 {g.size} 个" if g.size > 6 else ""
            lines.append(f"- 组 {i}（{REL_LABEL.get(g.rel, g.rel)}）：{names}{more}")
        if not low:
            lines.append("- 无")

        md_path.write_text("\n".join(lines), encoding="utf-8")

        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["序号", "动作", "文件路径", "大小(字节)", "方式", "所在组"])
            for i, r in enumerate(self.deleted, 1):
                w.writerow([i, "删除", str(r.path), r.size, r.mode, r.group_id])
            for a, b in self.kept_both:
                w.writerow(["", "都保留", str(self.result.files[a].path), "", "", ""])
                w.writerow(["", "都保留", str(self.result.files[b].path), "", "", ""])
        return md_path, csv_path


def restore_from_trash(path: Path) -> bool:
    """尽力从 Windows 回收站还原指定文件；做不到就返回 False。

    两个坑（都实测过）：
    1. 回收站里的项目，``Item.Path`` 指向 ``$Recycle.Bin\\$Rxxxxxx``，**不是**原位置，
       拿它跟原路径比永远不相等 —— 必须用「删除前所在目录
       ``System.Recycle.DeletedFrom``」拼上文件名来配对。
    2. 谓语（verb）的规范名是 ``undelete``；中文资源里显示为「还原」，
       直接 ``InvokeVerb('还原')`` 在英文谓语环境下不生效，两个都试才稳。
    同名文件被删过多次时，取最后删的那一份（按 DateDeleted）。
    """
    if os.name != "nt":
        return False
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$t=[IO.Path]::GetFullPath($env:PD_RESTORE);"
        "$shell=New-Object -ComObject Shell.Application;"
        "$rb=$shell.Namespace(10);"
        "$hit=$null;$best=$null;"
        "foreach($i in $rb.Items()){"
        "  try{"
        "    $o=$i.ExtendedProperty('System.Recycle.DeletedFrom');"
        "    if(-not $o){continue}"
        "    if([IO.Path]::GetFullPath([IO.Path]::Combine($o,$i.Name)) -ne $t){continue}"
        "    $d=$i.ExtendedProperty('System.Recycle.DateDeleted');"
        "    if(($hit -eq $null) -or ($d -gt $best)){$hit=$i;$best=$d}"
        "  }catch{}"
        "};"
        "if($hit -eq $null){Write-Output 'NOTFOUND';exit 2};"
        "foreach($v in @('undelete','还原','restore')){"
        "  try{$hit.InvokeVerb($v);Start-Sleep -Milliseconds 400}catch{};"
        "  if(Test-Path -LiteralPath $t){Write-Output 'OK';exit 0}"
        "};"
        "Write-Output 'FAIL';exit 3"
    )
    try:
        env = dict(os.environ, PD_RESTORE=str(path))
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return cp.returncode == 0
    except Exception:
        return False
