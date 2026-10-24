"""逐段目标行程与容差分桶单测：密度百分位 + 桶下缘点位 + 桶间宽→窄。"""

from __future__ import annotations

from app.executor.pen_order import target_fractions, width_buckets


def test_target_fractions_monotonic_by_density():
    dense = [[(x, 10) for x in range(0, 20)]] + [
        [(x, y) for x in range(0, 20)] for y in range(8, 30)  # 22 条平行线：高密度簇
    ]
    sparse = [[(0, 200), (50, 200)]]  # 远离簇的孤线：低密度
    fr = target_fractions(dense + sparse)
    assert fr[-1] < 0.1  # 孤线 → 行程最低（最细）
    assert max(fr[:-1]) > 0.9  # 簇心笔画 → 接近满行程
    assert len(fr) == len(dense) + len(sparse)


def test_width_buckets_ordering_and_coverage():
    paths = [[(i, 5), (i + 1, 6)] for i in range(0, 100, 10)]
    buckets = width_buckets(paths, 0.05)
    assert [b[0] for b in buckets] == sorted([b[0] for b in buckets], reverse=True)
    assert sum(len(b[2]) for b in buckets) == len(paths)
    for bucket_id, edge, _ in buckets:
        assert abs(edge - bucket_id * 0.05) < 1e-9  # 点位取桶下缘


def test_width_buckets_tol_bounds():
    # tol=0.05 → 桶号 ≤ 19；空桶跳过
    paths = [[(0, y), (30, y)] for y in (10, 200, 400)]
    buckets = width_buckets(paths, 0.05)
    assert all(0 <= b[0] <= 19 for b in buckets)
