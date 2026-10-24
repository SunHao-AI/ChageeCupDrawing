"""tier_groups 单测：局部线密度分档 + 「信号退化时原样返回」保证既有顺序逐位不变。"""

from __future__ import annotations

from app.executor.pen_order import tier_groups


def _paths() -> list[list[tuple[float, float]]]:
    # 密集区：8 条横线挤在 16px 高度内（线密度高=暗部）；稀疏区：两条远离彼此。
    dense = [[(10 + i, 10 + j * 2) for i in range(40)] for j in range(8)]
    sparse = [[(200 + i, 200) for i in range(40)], [(200, 300 + i) for i in range(40)]]
    return dense + sparse


def test_tiers_split_dense_vs_sparse() -> None:
    paths = _paths()
    groups = tier_groups(paths, 2)
    assert groups, "应有分组"
    assert groups[0][0] == 1  # 粗档先绘制
    # 稀疏区的两条线密度最低，绝不该进粗档。
    fine = groups[-1][1]
    assert paths[-2] in fine and paths[-1] in fine
    total = sum(len(group) for _, group in groups)
    assert total == len(paths)


def test_all_paths_covered_and_tiers_descend() -> None:
    paths = _paths()
    groups = tier_groups(paths, 3)
    total = sum(len(group) for _, group in groups)
    assert total == len(paths)
    tiers = [tier for tier, _ in groups]
    assert tiers == sorted(tiers, reverse=True)  # 粗档先绘制


def test_degenerate_signal_keeps_original_order() -> None:
    # 单条路径无区分度 → 单档原样返回。
    single = [[(i, i) for i in range(50)]]
    groups = tier_groups(single, 4)
    assert groups == [(0, single)]
