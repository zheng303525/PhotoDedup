"""候选生成与分组：多索引哈希（MIH）+ 并查集。

为什么需要候选生成：N 张图两两比较是 O(N²)，一万张就是 5000 万次比对。
MIH 把 64 位哈希切成 4 段 16 位，按「至少有一段差异很小」做索引，
把候选量压到线性附近，再用精确汉明距离复核。
"""

from __future__ import annotations

from itertools import combinations

# 分 4 段，每段 16 位
SEGMENTS = 4
SEG_BITS = 16
SEG_MASK = (1 << SEG_BITS) - 1


def masks_upto(k: int) -> list[int]:
    """所有 popcount <= k 的 16 位掩码。"""
    out = [0]
    idx = list(range(SEG_BITS))
    for size in range(1, k + 1):
        for combo in combinations(idx, size):
            m = 0
            for bit in combo:
                m |= 1 << bit
            out.append(m)
    return out


def candidate_pairs(variant_lists: list[list[int]], threshold: int,
                    max_pairs: int = 2_000_000) -> tuple[set[tuple[int, int]], int]:
    """返回所有可能落在阈值内的 (i, j) 候选对。

    variant_lists[i] 是第 i 张图的全部哈希变体（主指纹 + 旋转镜像）。
    数学依据：两哈希总距离 t <= 4k+3 时，必有一段差异 <= k，
    因此只查段内距离 <= k 的桶不会漏。
    """
    k = max(0, (threshold - 3 + 3) // 4)  # ceil((t-3)/4)
    k = min(k, 3)
    masks = masks_upto(k)

    buckets: dict[tuple[int, int], list[int]] = {}
    for i, hs in enumerate(variant_lists):
        for h in hs:
            for s in range(SEGMENTS):
                buckets.setdefault((s, (h >> (SEG_BITS * s)) & SEG_MASK), []).append(i)

    cand: set[tuple[int, int]] = set()
    for i, hs in enumerate(variant_lists):
        for h in hs:
            for s in range(SEGMENTS):
                seg = (h >> (SEG_BITS * s)) & SEG_MASK
                for m in masks:
                    for j in buckets.get((s, seg ^ m), ()):
                        if j > i:
                            cand.add((i, j))
        if len(cand) > max_pairs:
            break
    return cand, k


def unique_pairs(lists: list[list[int]]) -> list[list[int]]:
    """把变体列表去重（主指纹通常等于第一个变体）。"""
    out = []
    for hs in lists:
        seen, keep = set(), []
        for h in hs:
            if h not in seen:
                seen.add(h)
                keep.append(h)
        out.append(keep)
    return out


class DSU:
    """并查集：把「同一张」的关系传递成组。"""

    def __init__(self, n: int) -> None:
        self.p = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True


def collect_groups(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    """按并查集结果收集成员数 >= 2 的组。"""
    dsu = DSU(n)
    for a, b in edges:
        dsu.union(a, b)
    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(dsu.find(i), []).append(i)
    return [sorted(v) for v in buckets.values() if len(v) > 1]
