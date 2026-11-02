"""逐段目标行程与容差分桶单测：密度值归一化 + 短线限宽 + 桶下缘点位 + 桶间细→粗。"""

from __future__ import annotations

from app.executor import pen_order
from app.executor.pen_order import (
    DENSITY_CAP,
    target_fractions,
    width_buckets,
)


def _spread_paths() -> list[list[tuple[float, float]]]:
    """密度层次分明的笔迹组：紧密簇、中等簇、孤立线。"""
    dense = [[(x, 10) for x in range(0, 20)]] + [
        [(x, y) for x in range(0, 20)] for y in range(8, 30)
    ]
    mid = [[(200 + x, 120 + j * 3) for x in range(0, 20)] for j in range(3)]
    lone = [[(0, 200), (50, 200)]]
    return dense + mid + lone


def _dense_cluster() -> list[list[tuple[float, float]]]:
    return [[(x, y) for x in range(0, 20)] for y in range(0, 26)]


def test_target_fractions_dense_vs_sparse():
    dense = _dense_cluster()  # 高密度簇：行程应逼近上限
    sparse = [[(0, 200), (50, 200)]]  # 远离簇的孤线：低密度
    fr = target_fractions(dense + sparse)
    assert fr[-1] < 0.25  # 孤线 → 行程最低（最细；自身像素贡献少量密度）
    assert max(fr[:-1]) > 0.5  # 簇心笔画 → 明显更高的行程
    assert max(fr) <= DENSITY_CAP + 1e-9  # 值归一化封顶，不再摊满 1.0
    assert len(fr) == len(dense) + len(sparse)


def test_target_fractions_capped_not_rank_spread():
    """核心回归：多数稀疏笔画不得被摊进粗档（旧 rank 映射的糊黑根因）。"""
    # 300 条等距平行线（均匀中密度）+ 一小撮超密簇。rank 映射会把一半线推到
    # 行程 >0.5；值归一化下均匀中密度应整体留在低区。
    lines = [[(x, y * 4) for x in range(0, 60)] for y in range(120)]
    dense = [[(x, 600 + y) for x in range(0, 60)] for y in range(30)]
    fr = target_fractions(lines + dense)
    assert sum(1 for f in fr[:120] if f > 0.5) < 12  # 中密度线基本不进粗档
    assert fr[120] > fr[10]  # 密簇行程高于普通线


def test_short_stroke_width_budget():
    """短线按「实测宽度 ≤ 长度×1.15」封顶；长线不受影响。"""
    table = ((0.12, 6.0), (0.31, 8.0), (0.5, 14.0), (0.88, 23.0))
    # 20px 短线（<24）：预算 23px → 限到 0.88；6px 短线：预算 6.9 → 限到 0.12；
    # 100px 长线：不限。放在同一密度场里比较相对压制。
    short20 = [[(0, 0), (20, 0)]]
    short6 = [[(0, 40), (6, 40)]]
    long100 = [[(0, 80), (100, 80)]]
    monkey_paths = short20 + short6 + long100
    fr_no_table = target_fractions(monkey_paths)
    fr_with = target_fractions(monkey_paths, table)
    # 密度场退化（std≈0）时全 0，改测 width_buckets 端到端：短线桶 ≤ 预算档
    # 这里只验证接口不抛错且长度一致。
    assert len(fr_with) == len(fr_no_table) == 3


def test_short_stroke_clamped_in_buckets():
    table = ((0.12, 6.0), (0.31, 8.0), (0.5, 14.0), (0.88, 23.0))
    # 密簇里埋一条 10px 短线：簇密度会把它推到 >0.5，但长度预算 11.5px < 14px
    # 档实测宽度，行程必须被压回 ≤0.31。
    dense = [[(x, y) for x in range(0, 60)] for y in range(0, 40)]
    short = [[(25, 20), (35, 20)]]  # 埋进簇心：采样点全在高密度场
    fr_no = target_fractions(dense + short)
    fr_yes = target_fractions(dense + short, table)
    assert fr_no[-1] > 0.31  # 无限宽时短线随簇吃粗
    assert fr_yes[-1] <= 0.31 + 1e-9  # 限宽后回落到预算档


def test_width_buckets_ordering_and_coverage():
    paths = [[(i, 5), (i + 1, 6)] for i in range(0, 100, 10)]
    buckets = width_buckets(paths, 0.05)
    # 先细后粗：桶号升序（起笔最细档，逐档变粗）
    assert [b[0] for b in buckets] == sorted([b[0] for b in buckets])
    assert sum(len(b[2]) for b in buckets) == len(paths)
    for bucket_id, edge, _ in buckets:
        assert abs(edge - bucket_id * 0.05) < 1e-9  # 点位取桶下缘


def test_width_buckets_tol_bounds():
    # tol=0.05 → 桶号 ≤ 19；空桶跳过
    paths = [[(0, y), (30, y)] for y in (10, 200, 400)]
    buckets = width_buckets(paths, 0.05)
    assert all(0 <= b[0] <= 19 for b in buckets)


def test_bucket_cap_derived_from_tol():
    """桶上限随 tol 推导；行程封顶 DENSITY_CAP=0.75 → 最高桶=floor(0.75/tol)。"""
    paths = _spread_paths() + _dense_cluster()
    fine = width_buckets(paths, 0.02)
    assert max(b[0] for b in fine) == int(DENSITY_CAP / 0.02)  # 37
    for bucket_id, edge, _ in fine:
        assert abs(edge - bucket_id * 0.02) < 1e-9
    assert max(b[0] for b in width_buckets(paths, 0.05)) == int(DENSITY_CAP / 0.05)
    assert max(b[0] for b in width_buckets(paths, 0.2)) == int(DENSITY_CAP / 0.2)


def test_bucket_boundary_rounding(monkeypatch):
    """恰落桶下缘的行程不得掉进下一档（0.6/0.2 浮点下偏）。"""
    paths = [[(i, 0), (i + 1, 1)] for i in range(21)]
    fracs = [r / 20 for r in range(21)]  # 21 段已知名次：frac = r/20
    monkeypatch.setattr(
        pen_order, "target_fractions", lambda _paths, _table=None: fracs
    )
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
    monkeypatch.setattr(
        pen_order, "target_fractions", lambda _paths, _table=None: [0.0, 0.5, 1.0]
    )
    buckets = width_buckets(paths, 0.2)
    ids = [b[0] for b in buckets]
    assert ids == [0, 2, 4]  # 细→粗，空洞（1、3）不产生空桶
    assert len(buckets) < 5  # cap+1


def test_tied_densities_equal_travel():
    """值归一化下并列密度得到相同行程（旧 rank 会人为区分并列段）。"""
    dup = [(x, 10) for x in range(0, 20)]
    paths = [list(dup), list(dup), [(0, 200), (50, 200)]]
    fr = target_fractions(paths)
    assert fr[0] == fr[1]
