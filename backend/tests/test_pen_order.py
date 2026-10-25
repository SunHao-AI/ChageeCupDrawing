"""逐段目标行程与容差分桶单测：密度百分位 + 桶下缘点位 + 桶间宽→窄。"""

from __future__ import annotations

from app.executor import pen_order
from app.executor.pen_order import target_fractions, width_buckets


def _spread_paths() -> list[list[tuple[float, float]]]:
    """密度层次分明的笔迹组：紧密簇、中等簇、孤立线，覆盖满行程。"""
    dense = [[(x, 10) for x in range(0, 20)]] + [
        [(x, y) for x in range(0, 20)] for y in range(8, 30)
    ]
    mid = [[(200 + x, 120 + j * 3) for x in range(0, 20)] for j in range(3)]
    lone = [[(0, 200), (50, 200)]]
    return dense + mid + lone


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


def test_bucket_cap_derived_from_tol():
    """桶上限随 tol 推导：0.02→49、0.05→19、0.2→4；细粒度下缘可达 ≥0.9。"""
    paths = _spread_paths()
    fine = width_buckets(paths, 0.02)
    assert max(b[0] for b in fine) == 49  # 硬编码 19 会把 62% 以上行程挤进一桶
    assert max(b[1] for b in fine) >= 0.9
    for bucket_id, edge, _ in fine:
        assert abs(edge - bucket_id * 0.02) < 1e-9
    assert max(b[0] for b in width_buckets(paths, 0.05)) == 19
    assert max(b[0] for b in width_buckets(paths, 0.2)) == 4


def test_bucket_boundary_rounding(monkeypatch):
    """恰落桶下缘的行程不得掉进下一档（0.6/0.2 浮点下偏）。"""
    paths = [[(i, 0), (i + 1, 1)] for i in range(21)]
    fracs = [r / 20 for r in range(21)]  # 21 段已知名次：frac = r/20
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: fracs)
    buckets = width_buckets(paths, 0.2)
    at = {id(path): bucket_id for bucket_id, _, group in buckets for path in group}
    assert at[id(paths[4])] == 1  # 0.2
    assert at[id(paths[8])] == 2  # 0.4
    assert at[id(paths[12])] == 3  # 0.6
    assert at[id(paths[16])] == 4  # 0.8


def test_degenerate_signal_all_zero():
    single = [[(i, i) for i in range(50)]]
    assert target_fractions(single) == [0.0]
    assert width_buckets(single, 0.1) == [(0, 0.0, single)]
    same = [list(single[0]), list(single[0])]
    assert target_fractions(same) == [0.0, 0.0]


def test_buckets_are_pure_and_reproducible():
    paths = _spread_paths()
    assert target_fractions(paths) == target_fractions(paths)
    assert width_buckets(paths, 0.05) == width_buckets(paths, 0.05)


def test_tol_clamped_and_nan_fallback():
    paths = _spread_paths()
    assert width_buckets(paths, 0.0) == width_buckets(paths, 0.02)
    assert width_buckets(paths, -1.0) == width_buckets(paths, 0.02)
    assert width_buckets(paths, 5.0) == width_buckets(paths, 0.2)
    assert width_buckets(paths, float("nan")) == width_buckets(paths, 0.05)


def test_empty_inputs():
    assert target_fractions([]) == []
    assert width_buckets([], 0.05) == []
    assert target_fractions([[]]) == []  # 空路径入口过滤
    assert width_buckets([[], []], 0.05) == []


def test_empty_buckets_skipped(monkeypatch):
    paths = [[(i, 0), (i + 1, 1)] for i in range(3)]
    monkeypatch.setattr(pen_order, "target_fractions", lambda _paths: [0.0, 0.5, 1.0])
    buckets = width_buckets(paths, 0.2)
    ids = [b[0] for b in buckets]
    assert ids == [4, 2, 0]  # 宽→窄，空洞（3、1）不产生空桶
    assert len(buckets) < 5  # cap+1


def test_tied_densities_stable_order():
    dup = [(x, 10) for x in range(0, 20)]
    paths = [list(dup), list(dup), [(0, 200), (50, 200)]]
    fr = target_fractions(paths)
    assert fr == [0.0, 0.5, 1.0]  # 并列密度按原始顺序取相邻名次
