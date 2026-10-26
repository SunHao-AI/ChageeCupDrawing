# 逐段笔宽控制实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每段笔画获得连续目标笔宽，通过控件行为探测 + 双态工具位图 + 容差合批实现「先粗后细、逐段变宽、绝不误用橡皮」的自动绘制。

**Architecture:** 标定阶段用行为探测（点击→观测画布/工具条变化）自动学出画笔/橡皮/滑杆/撤销/折叠五个控件在面板展开态与折叠态的坐标，以及画笔选中态指纹；规划阶段按线密度百分位给每段笔画分配目标滑杆行程；执行阶段按容差分桶合批，仅在跨桶时展开面板、校验画笔态、点滑杆、折叠。

**Tech Stack:** Python 3.12 / FastAPI / OpenCV + NumPy / pytest（后端）；Vue 3 + TS（前端参数项）；SQLite（canvas_profile 表）。

**Spec:** [docs/per-stroke-width-design.md](../per-stroke-width-design.md)

## Global Constraints

- 全量 `pytest` 通过后才允许提交（当前基线 609 项）。
- 鼠标/截图一律经 `DrawingTarget` 抽象（`capture_image` / `foreground_matches` / `activate` / `client_area`），不直接触碰 Win32。
- 探测与换笔的所有鼠标动作前必须 `foreground_matches()`，否则 `activate()`（沿用 `_ensure_foreground` 模式）。
- 所有探测点击必须落在画布矩形之外；墨迹判定只针对画布顶部「刻度带」。
- 绘制全程永不点击橡皮按钮。
- 旧校准记录（无新字段）必须能继续工作：回退单套坐标旧路径 + `task.warning`，不得报错。
  - 安全侧例外（2026-10-26 产品裁决，维持实现现状）：画笔校验中「点击画笔按钮后复检仍非画笔」视为工具条不可信，报 `E_NOT_BRUSH` 终止任务而非仅 warning——本约束的「不得报错」只覆盖坐标缺失/降级路径，不含此场景。第一优先级是绝不用橡皮擦用户画作（设计文档 §2「绝不下笔」）。
- `widthTolerance` 取值 0.02–0.20，默认 0.05；`autoWidth` 默认值改为 `True`。
- 后端代码注释用中文，与现有风格一致。

## File Structure

| 文件 | 职责 |
| --- | --- |
| Create `backend/app/executor/toolbar_probe.py` | 行为探测：`ToolBitmap` 数据类 + `ToolbarProber`（折叠→滑杆→撤销→橡皮/画笔 顺序探测，含画笔指纹采集） |
| Modify `backend/app/store/models.py` | `CanvasProfile` 增加 `tools_open` / `tools_collapsed` 字段 |
| Modify `backend/app/store/db.py` | 迁移补列 `tools_open_json` / `tools_collapsed_json` |
| Modify `backend/app/store/store.py` | `save_calibration` / 行映射读写新字段 |
| Modify `backend/app/api/schemas.py` | `CalibrationPost` 增加 `toolsOpen` / `toolsCollapsed` 嵌套模型 |
| Modify `backend/app/api/routes_projects.py` | 校准 POST/GET 透传新字段 |
| Modify `backend/app/executor/pen_order.py` | 新增 `width_buckets()`（百分位目标行程 + 容差分桶 + 桶内排序 + 桶间宽→窄）；`tier_groups` 删除 |
| Modify `backend/app/executor/pen_calibration.py` | 清理判定改「相对基线」（`baseline_occupied`） |
| Modify `backend/app/executor/service.py` | `ExecParams` 参数改造；标定后跑探测；双态坐标选择；画笔校验；跨桶换宽；续画重排对齐 |
| Create `backend/tests/test_toolbar_probe.py` | 探测各分支单测（合成工具栏 FakeBoard） |
| Modify `backend/tests/test_pen_order.py` | 新增分桶用例；删除旧 tier 用例 |
| Modify `backend/tests/test_pen_calibration.py` | 基线判定用例 |
| Modify `backend/tests/test_exec_tiered_width.py` | 容差合批 / 画笔校验 / 双态坐标 / 续画对齐 / 旧记录回退 |
| Modify `backend/app/api/routes_tasks.py`（如错误码需登记） | `E_NOT_BRUSH` 走既有 TaskError 通道，通常无需改 |
| Modify `frontend/src/params/registry.ts` | `widthTiers`→`widthTolerance`（0.02–0.20 步进 0.01），`autoWidth` 默认 true |
| Modify `frontend/src/api/types.ts` | `ExecParams` 类型同步 |
| Modify `docs/open-items.md` | 真机验证项挂 §1A 族；§0 快照更新 |

---

### Task 1: 数据层——双态工具位图存储

**Files:**
- Modify: `backend/app/store/models.py`（`CanvasProfile`）
- Modify: `backend/app/store/db.py`（迁移列表）
- Modify: `backend/app/store/store.py`（`save_calibration`、行→模型映射约 590-615 行）
- Test: `backend/tests/test_store_migrations.py`（已存在，追加）

**Interfaces:**
- Produces: `CanvasProfile.tools_open: dict | None`、`CanvasProfile.tools_collapsed: dict | None`（形状：`{"brush":{"x":int,"y":int},"eraser":{...},"slider":{"x":int,"y":int,"w":int,"h":int}|None,"undo":{...}|None,"fold":{...},"brushSelectedLum":float,"brushIdleLum":float}`）；`save_calibration(..., tools_open: dict | None = None, tools_collapsed: dict | None = None)`

- [ ] **Step 1: 写失败测试** — `test_store_migrations.py` 末尾追加：

```python
def test_tools_bitmaps_roundtrip(tmp_path):
    store = Store(tmp_path / "app.db")
    store.create_project("p1", "s.png", "img")
    bit = {"brush": {"x": 10, "y": 500}, "eraser": {"x": 60, "y": 500},
           "slider": {"x": 100, "y": 520, "w": 200, "h": 12}, "undo": None,
           "fold": {"x": 300, "y": 500}, "brushSelectedLum": 180.0,
           "brushIdleLum": 250.0}
    store.save_calibration(
        project_id="p1", hwnd=1, window_title="t", capture_method="wgc",
        rect_x=0, rect_y=0, rect_w=100, rect_h=100, dpi_scale=1.0,
        source="manual", confidence=1.0, tools_open=bit, tools_collapsed=None,
    )
    p = store.latest_calibration("p1")
    assert p.tools_open == bit
    assert p.tools_collapsed is None
```

注意：`save_calibration` 的实际参数签名以现有代码为准（位置参数或关键字按现状传入，其余字段照 `test_store_migrations.py` 已有用例的写法）。

- [ ] **Step 2: 跑测试确认失败**（`TypeError: save_calibration() got an unexpected keyword argument 'tools_open'`）

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_store_migrations.py -x -q`

- [ ] **Step 3: 实现**
  1. `models.py` `CanvasProfile` 在 `fold` 字段后加：

```python
    # 面板展开/折叠两态工具位图（ToolbarProber 行为探测产物，人工兜底可覆盖单项）。
    tools_open: dict | None = None
    tools_collapsed: dict | None = None
```

  2. `db.py` 迁移列表（与 `("slider_json", "TEXT")` 同一列表）追加 `("tools_open_json", "TEXT")`、`("tools_collapsed_json", "TEXT")`。
  3. `store.py`：`save_calibration` 签名加两个关键字参数、构造函数传参、INSERT 列与占位符各加两个、`json.dumps(tools_open) if tools_open else None` 依葫芦画瓢；`_row_to_profile`（约 590-615 行）对称读取 `tools_open_json` / `tools_collapsed_json` 并 `json.loads`。

- [ ] **Step 4: 跑测试通过** — Run: `pytest tests/test_store_migrations.py tests/test_contracts.py -q`（后者覆盖校准接口回归）
- [ ] **Step 5: 提交** — `git add backend/app/store backend/tests/test_store_migrations.py; git commit -m "feat(store): 校准记录增加展开/折叠双态工具位图字段"`

---

### Task 2: API 层——校准接口透传新字段

**Files:**
- Modify: `backend/app/api/schemas.py`（`CalibrationPost` 约 69-72 行附近）
- Modify: `backend/app/api/routes_projects.py`（POST 146-160 行、GET 175-177 行）
- Test: `backend/tests/test_contracts.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `save_calibration(tools_open=..., tools_collapsed=...)`
- Produces: `POST /api/projects/{pid}/calibration` 接受请求体 `toolsOpen` / `toolsCollapsed`（对象，内部字段不强 schema 校验、原样透传入库）；项目快照响应回传两字段

- [ ] **Step 1: 写失败测试** — `test_contracts.py` 追加（沿用该文件既有 client fixture 命名）：

```python
def test_calibration_accepts_tools_bitmaps(client):
    pid = _make_project(client)  # 沿用该文件既有的建项目辅助函数/写法
    body = {...同该文件现有 calibration POST 用例的必填字段...,
            "toolsOpen": {"brush": {"x": 10, "y": 500}, "brushSelectedLum": 180.0},
            "toolsCollapsed": None}
    r = client.post(f"/api/projects/{pid}/calibration", json=body)
    assert r.status_code == 200
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["calibration"]["toolsOpen"]["brush"]["x"] == 10
    assert snap["calibration"]["toolsCollapsed"] is None
```

- [ ] **Step 2: 跑测试确认失败** — Run: `pytest tests/test_contracts.py -x -q`（422 或字段缺失）
- [ ] **Step 3: 实现** — `schemas.py` 的 `CalibrationPost` 增加 `toolsOpen: dict | None = None`、`toolsCollapsed: dict | None = None`（探测产物结构由 executor 保证，API 层用 `dict` 透传即可，不重复建嵌套模型）；`routes_projects.py` POST 保存时传 `tools_open=payload.toolsOpen, tools_collapsed=payload.toolsCollapsed`，GET 响应 `"toolsOpen": profile.tools_open, "toolsCollapsed": profile.tools_collapsed`。
- [ ] **Step 4: 跑测试通过** — Run: `pytest tests/test_contracts.py -q`
- [ ] **Step 5: 前端类型同步** — `frontend/src/api/types.ts` 校准类型补 `toolsOpen?: Record<string, unknown> | null`、`toolsCollapsed?: ... | null`（与现有 `slider/undo/fold` 字段并列处）。
- [ ] **Step 6: 构建验证 + 提交** — Run: `cd frontend; npm run build`（通过即可）；`git add backend/app/api backend/tests/test_contracts.py frontend/src/api/types.ts; git commit -m "feat(api): 校准接口透传双态工具位图"`

---

### Task 3: ToolbarProber——控件行为探测

**Files:**
- Create: `backend/app/executor/toolbar_probe.py`
- Test: `backend/tests/test_toolbar_probe.py`

**Interfaces:**
- Consumes: `SliderProbe` 协议（`capture_image/foreground_matches/activate`，同 `pen_calibration.py:33`）、鼠标 duck-type（`move_to/down/up/click`）、`to_screen` 换算
- Produces:

```python
@dataclass(frozen=True)
class ToolBitmap:
    brush: tuple[float, float] | None
    eraser: tuple[float, float] | None
    slider: dict | None          # {"x","y","w","h"}
    undo: tuple[float, float] | None
    fold: tuple[float, float] | None
    brush_selected_lum: float | None   # 画笔格选中态背景亮度
    brush_idle_lum: float | None

class ProbeError(Exception):
    code: str  # E_STOPPED / E_PROBE_NO_TOOLBAR / E_PROBE_NO_SLIDER

class ToolbarProber:
    def __init__(self, *, probe: SliderProbe, mouse: object,
                 to_screen: Callable[[float, float], tuple[float, float]],
                 sleep: Callable[[float], None] = time.sleep,
                 settle_s: float = 0.45) -> None: ...
    def run(self, *, canvas: tuple[int, int, int, int], band: tuple[int, int, int, int],
            open_bitmap: "ToolBitmap | None",  # 人工兜底提供的已知项（优先级最高）
            on_progress: Callable[[str], None],
            should_stop: Callable[[], bool]) -> ToolBitmap
```

`open_bitmap` 用于注入人工兜底已知项（如旧的单套 slider 坐标）：非 None 的字段跳过探测直接采用。

- [ ] **Step 1: 写失败测试（合成画板）** — 新建 `tests/test_toolbar_probe.py`，扩展复用 `test_pen_calibration.py` 的 FakeBoard/FakeMouse 模式：

```python
class ToolbarBoard(FakeBoard):
    """合成工具栏：grid 上画折叠钮/画笔格/橡皮格/撤销格；panel_open 决定
    滑杆是否可见与工具格亮度；handle_click 按控件矩形派发状态迁移。"""
    def __init__(self):
        super().__init__()
        self.panel_open = True
        self.tool = "brush"      # 当前选中工具
        # 控件矩形（截图坐标）：fold(300,500,24,24) brush(10,495,40,40)
        # eraser(60,495,40,40) undo(110,495,40,40) slider(100,530,200,12)
    def render(self):
        # 依 panel_open/tool 重画工具条：选中格填充亮度 180（暗），未选 250
        # panel_open=False 时滑杆轨道不画、工具格上移 40px
```

用例（每个先断言失败）：

```python
def test_probe_finds_fold_and_slider():        # 折叠按钮点击使轨道消失→定位成功
def test_probe_finds_undo_by_ink_drop():       # 刻度带基线+点击候选→墨量降→undo
def test_probe_finds_eraser_by_line_erase():   # 压线重描线变白→eraser
def test_probe_brush_fingerprints():           # 选中/未选中画笔格亮度差>30
def test_probe_respects_manual_items():        # open_bitmap 已给的字段不再探测
def test_probe_clicks_only_outside_canvas():   # 全程点击 y >= canvas 下缘
def test_probe_stops_on_should_stop():         # ProbeError E_STOPPED
```

- [ ] **Step 2: 跑测试确认失败** — Run: `pytest tests/test_toolbar_probe.py -x -q`（ModuleNotFoundError）
- [ ] **Step 3: 实现 `toolbar_probe.py`**（顺序与判据按 spec §1；核心骨架）：

```python
"""工具栏行为探测：不依赖图标模板，靠「点击→观测」学出控件位置。

顺序：折叠 → 滑杆 → 撤销 → 橡皮/画笔。折叠先于滑杆（折叠态轨道不可见），
撤销先于橡皮/画笔（清理依赖撤销）。所有点击落在画布外，墨迹观测只看刻度带。
画笔「选中/未选中」两态背景亮度差即选中指纹，供绘制期校验当前工具态。
"""

class ToolbarProber:
    BAND_LINE_LEN = 46
    LUM_DELTA_MIN = 30          # 选中态判定最小亮度差
    CANDIDATE_STEP = 28         # 工具条候选点网格步长

    def run(self, *, canvas, band, open_bitmap, on_progress, should_stop) -> ToolBitmap:
        # 1) fold：工具条区域候选点逐个 click，click 后 _slider_visible() 翻转者即折叠钮；
        #    click 两次应回到原态（验证可逆，防误点系统控件）。
        # 2) slider：确保展开（第 1 步已知 fold）；对轨道区域暗段做行投影，
        #    连续暗段宽 > 60px 且高 2~30px → {"x","y","w","h"}。
        # 3) undo：_draw_band_line() → 候选点逐个 click → _band_occupied() 显著下降
        #    （降到基线 40% 以下）者即撤销；点击后无法恢复的候选直接跳过（先试不可
        #    逆代价最低的策略：撤销本身可被「重新描线」重建再试，故 undo 探测前先
        #    记录线，失败候选后补描）。
        # 4) eraser/brush：带内有线 → 点候选 → 沿线重描 → 线白化(_band_line_erased)
        #    即橡皮；否则候选是其他工具（画笔画新线记入 strokes 并撤销清理）。
        # 5) brush 指纹：选中画笔态截 brush 格均值亮度 = selected；切到 eraser 再截 = idle。
        # 每个控件探到即 on_progress(f"已识别 {名称}")；全部失败抛 ProbeError。
```

实现细则：
- `_band_occupied()` / `_band_line_erased()` 复用 `ink_measure.ink_occupied_fraction` 与 `measure_ink_width`。
- 候选点 = 画布下缘+8 到窗口截图底缘之间、按 `CANDIDATE_STEP` 的网格，跳过画布矩形与刻度带内的点。
- 每步动作前 `self._ensure_foreground()`（模式同 `pen_calibration.py:278`）。
- `open_bitmap` 中非 None 字段直接拷贝进结果；`slider` 人工给定则跳过第 2 步但仍探 fold（若 fold 也缺）。

- [ ] **Step 4: 跑测试通过** — Run: `pytest tests/test_toolbar_probe.py -q`
- [ ] **Step 5: 提交** — `git add backend/app/executor/toolbar_probe.py backend/tests/test_toolbar_probe.py; git commit -m "feat(executor): 工具栏行为探测 ToolbarProber（折叠/滑杆/撤销/橡皮/画笔指纹）"`

---

### Task 4: 逐段目标笔宽与容差分桶（pen_order 重写）

**Files:**
- Modify: `backend/app/executor/pen_order.py`（保留 `_line_density_map`/`_path_density`/`DENSITY_WIN`，删除 `tier_groups`，新增 `target_fractions` + `width_buckets`）
- Test: `backend/tests/test_pen_order.py`（新增替换旧 `tier_groups` 用例）

**Interfaces:**
- Consumes: `Path = list[tuple[float, float]]`、`nearest_order_paths`
- Produces:

```python
def target_fractions(paths: list[Path]) -> list[float]:
    """每段笔画 → 目标滑杆行程 [0,1]（1=最粗）。密度百分位排名；信号退化时全 0。"""

def width_buckets(paths: list[Path], tol: float) -> list[tuple[int, float, list[Path]]]:
    """→ [(桶号, 桶下缘行程, 桶内笔迹)]，按桶号降序（宽→窄）。桶号=floor(f/tol)。"""
```

- [ ] **Step 1: 写失败测试**（`test_pen_order.py` 追加，随后删除引用 `tier_groups` 的旧用例）：

```python
def test_target_fractions_monotonic_by_density():
    dense = [[(x, 10) for x in range(0, 20)]] * 1 + [
        [(x, y) for x in range(0, 20)] for y in range(8, 30)  # 30 条平行线：高密度簇
    ]
    sparse = [[(0, 200), (50, 200)]]  # 远离簇的孤线：低密度
    fr = target_fractions(dense + sparse)
    assert fr[-1] < 0.1                      # 孤线 → 行程最低（最细）
    assert max(fr[:-1]) > 0.9                # 簇心笔画 → 接近满行程
    assert len(fr) == len(dense) + len(sparse)

def test_width_buckets_ordering_and_coverage():
    paths = [[(i, 5), (i + 1, 6)] for i in range(0, 100, 10)]
    buckets = width_buckets(paths, 0.05)
    assert all(b[0] for _, _, _ in [(x[0], x[1], x[2]) for x in buckets]) or True
    assert [b[0] for b in buckets] == sorted([b[0] for b in buckets], reverse=True)
    assert sum(len(b[2]) for b in buckets) == len(paths)
    for bucket_id, edge, _ in buckets:
        assert abs(edge - bucket_id * 0.05) < 1e-9   # 点位取桶下缘

def test_width_buckets_tol_bounds():
    # tol=0.05 → 桶号 ≤ 19；空桶跳过
    paths = [[(0, y), (30, y)] for y in (10, 200, 400)]
    buckets = width_buckets(paths, 0.05)
    assert all(0 <= b[0] <= 19 for b in buckets)
```

- [ ] **Step 2: 跑测试确认失败** — Run: `pytest tests/test_pen_order.py -x -q`
- [ ] **Step 3: 实现**：

```python
def target_fractions(paths: list[Path]) -> list[float]:
    if not paths:
        return []
    ys = [round(y) for p in paths for _, y in p]
    xs = [round(x) for p in paths for x, _ in p]
    box = _line_density_map(paths, (max(ys) + 2, max(xs) + 2))
    densities = np.array([_path_density(p, box) for p in paths], dtype=np.float64)
    if float(densities.std()) < 1e-6:
        return [0.0] * len(paths)          # 信号退化：全用最细档
    ranks = densities.argsort().argsort().astype(np.float64)
    return [float(r) / max(1, len(paths) - 1) for r in ranks]

def width_buckets(paths: list[Path], tol: float) -> list[tuple[int, float, list[Path]]]:
    tol = max(0.02, min(0.20, float(tol)))
    fracs = target_fractions(paths)
    groups: dict[int, list[Path]] = {}
    for frac, path in zip(fracs, paths):
        groups.setdefault(min(19, int(frac / tol)), []).append(path)
    out = []
    for bucket_id in sorted(groups, reverse=True):      # 宽→窄
        out.append((bucket_id, bucket_id * tol, nearest_order_paths(groups[bucket_id])))
    return out
```

注意 `test_width_buckets_ordering_and_coverage` 第一行是无意义断言，实现时删去（保留其余断言）。

- [ ] **Step 4: 跑测试通过** — Run: `pytest tests/test_pen_order.py -q`
- [ ] **Step 5: 提交** — `git add backend/app/executor/pen_order.py backend/tests/test_pen_order.py; git commit -m "feat(executor): 逐段百分位目标行程 + 容差分桶 width_buckets（替代固定 tier）"`

---

### Task 5: 标定清理判定改「相对基线」

**Files:**
- Modify: `backend/app/executor/pen_calibration.py`（`run()` 与 `_clear_canvas()`）
- Test: `backend/tests/test_pen_calibration.py`

**Interfaces:**
- Consumes/Produces: `PenCalibrator.run(..., baseline_occupied: float = 0.0)` — 新关键字参数，表示刻度带内**标定开始前**的既有墨量占比；`_clear_canvas` 判定条件由 `<= CLEAR_CONFIRM_OCCUPIED` 改为 `<= baseline_occupied + 0.003`

- [ ] **Step 1: 写失败测试**：

```python
def test_clear_accepts_preexisting_ink():
    """用户画布刻度带本就有墨（占比≈0.01）时，撤掉测试线即判清空成功。"""
    board = FakeBoard()
    board.grid[64:68, 46:92] = 0          # 基线墨迹（原函数会误判未清空）
    ...  # 复用文件内既有 harness 构造 PenCalibrator 并跑 run(baseline_occupied=0.01)
```

（沿用文件内 `_make_calibrator()` / 已有用例的组装方式，只多传 `baseline_occupied`。）

- [ ] **Step 2: 跑测试确认失败**（`E_CALIBRATION_NOT_CLEARED`）
- [ ] **Step 3: 实现** — `run()` 与 `_clear_canvas()` 各加 `baseline_occupied: float = 0.0` 参数并透传；判定改为 `ink_occupied_fraction(image, check) <= baseline_occupied + self.CLEAR_CONFIRM_OCCUPIED`。
- [ ] **Step 4: 跑测试通过** — Run: `pytest tests/test_pen_calibration.py -q`（确认旧用例默认基线 0 不受影响）
- [ ] **Step 5: 提交** — `git commit -am "fix(executor): 标定清空判定改为相对基线，画布本有墨不再误判失败"`

---

### Task 6: 执行器集成——参数改造 + 探测接入 + 双态坐标 + 画笔校验 + 跨桶换宽

**Files:**
- Modify: `backend/app/executor/service.py`
- Test: `backend/tests/test_exec_tiered_width.py`（现有文件扩展；沿用其 fake 组装方式）

**Interfaces:**
- Consumes: Task 1/2 的 `tools_open/tools_collapsed`；Task 3 的 `ToolbarProber.run(...)`；Task 4 的 `width_buckets(paths, tol)`；Task 5 的 `baseline_occupied`
- Produces: `ExecParams.width_tolerance: float = 0.05`、`auto_width: bool = True`（`from_dict` 忽略 `widthTiers`）；新错误码 `E_NOT_BRUSH`；绘制序列 `(bucket_id, target_frac, path)` 三元组

- [ ] **Step 1: 写失败测试**（追加到 `test_exec_tiered_width.py`；沿用文件内既有 fake target/mouse/probe 组装）：

```python
def test_buckets_switch_only_when_crossing():
    # 3 段线分落桶 19/19/18（tol=0.05）→ 展开面板点击恰 2 次，滑杆点 y=轨道中线、
    # x 分别 = track.x + w*0.95 与 *0.90（桶下缘）
def test_brush_state_enforced_before_first_stroke():
    # fake probe 初始工具=eraser：执行器必须先点 brush 再下笔；无法纠正→FAILED E_NOT_BRUSH
def test_never_clicks_eraser_during_draw():
    # 记录全部点击坐标，断言无一落入 eraser 热区
def test_uses_collapsed_bitmap_when_panel_folded(): ...
def test_resume_recomputes_identical_sequence():
    # 同一 plan+params 跑两次 _flatten，cursor 处截断后续段逐位相同
def test_legacy_profile_single_bitmap_fallback():
    # tools_* 均 None：沿用 calibration.slider 旧路径 + warning「旧校准」
def test_width_tiers_key_ignored():
    assert ExecParams.from_dict({"widthTiers": 3}).width_tolerance == 0.05
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（`service.py` 改造点逐项列出）：

1. `ExecParams`：`width_tiers` 字段删除 → `width_tolerance: float = 0.05`；`auto_width` 默认 `True`；`from_dict`：`width_tolerance=float(data.get("widthTolerance", 0.05))`，不再读 `widthTiers`；`as_dict` 对称。
2. `_flatten_tiered_paths` 重命名 `_build_draw_sequence`，返回 `list[tuple[int, float, list]]`：无宽度表 → `[(0, 0.0, p) for p in paths]`（单一 0 桶 = 最细档，行为同现状单细笔）；有宽度表 → `width_buckets(paths, params.width_tolerance)`。
3. `_arm_and_draw`：
   - 标定调用（约 426-434 行）不变，仅新增传 `baseline_occupied`：标定开始前先 `_grab` 一次算刻度带 `ink_occupied_fraction` 传入（Task 5 签名）。
   - **探测接入**：标定完成后，若 `calibration.tools_open` 为 None → 构造 `ToolbarProber` 跑探测（`open_bitmap` 传入由旧字段 `slider/fold/undo` 组装的部分位图作兜底），结果 `dataclasses.asdict` 化后 `store.save_calibration` 复制一条新 profile 持久化（沿用 `latest_calibration` 其余字段；探测失败仅 `task.warning`，回退旧路径，不阻断）。
   - **双态选择**：`_active_tools(panel_state)` helper —— 展开取 `tools_open`、折叠取 `tools_collapsed`；两者皆 None → 旧 `slider_now/fold_now` 单套路径 + warning「旧校准：无法校验画笔态」（`legacy_mode=True` 时画笔校验降级为宽松）。
   - **画笔校验**：`_ensure_brush(tools, slider_rect, mouse)` —— `legacy_mode` 或无指纹（`brushSelectedLum is None`）直接放行；否则对 `tools.brush` 格中心 8×8 区域取均值亮度 `lum`，`lum >= (selected+idle)/2` 判选中，非选中则点 brush + 复检；仍失败 → `raise TaskError(409, "E_NOT_BRUSH", "画板当前不是画笔工具且无法自动切换，已停止未下笔")`。调用点：首笔前（fold 后、DRAWING 前）一次 + 每次展开面板换宽时一次。
   - **跨桶换宽**（替换 451-486 行 tier 分支）：循环解包 `bucket, target_frac, canvas_path`；`bucket != active_bucket` 时——`panel_active` 则展开（失败沿用现宽 warning，同现状）→ `_ensure_brush` → 点滑杆 `sx = slider.x + slider.w * min(target_frac + tol/2, 1.0)`（桶中心偏上 tol/2 处，与「实际不粗于计划」协同：点位取 `[下缘, 上缘]` 中值，spec §4 定案为**桶下缘**——点位 `target_frac` 即下缘，见 Task 4 产出语义，直接 `sx = slider.x + slider.w * target_frac`）→ `time.sleep(0.25)` → `active_bucket = bucket` → `panel.fold`。warning 文案「已切换第 {bucket+1} 档笔宽（目标行程 {target_frac:.2f}）」。
   - `flat = self._build_draw_sequence(task_id, paths, params)`；游标语义、SSE、守护、断点续画逻辑不动（序列确定性由 Task 4 纯函数保证）。

- [ ] **Step 4: 跑本文件测试通过** — Run: `pytest tests/test_exec_tiered_width.py -q`
- [ ] **Step 5: 全量回归** — Run: `pytest -q`；Expected: 全绿（609 基线 + 新增；删掉的 tier_groups 用例数抵消后如数报告）
- [ ] **Step 6: 提交** — `git add backend/app/executor/service.py backend/tests/test_exec_tiered_width.py; git commit -m "feat(executor): 容差合批换宽 + 双态位图 + 画笔态强校验 + 探测持久化"`

---

### Task 7: 前端参数与默认值

**Files:**
- Modify: `frontend/src/params/registry.ts`（170-171、216-217 行附近）
- Modify: `frontend/src/api/types.ts`（43-45 行附近）
- Modify: `frontend/src/views/RunView.vue`（132-135 行提示文案微调）

**Interfaces:**
- Consumes: Task 6 的 `widthTolerance` JSON 字段
- Produces: 参数面板「换宽粒度」滑杆（0.02–0.20，step 0.01，默认 0.05）；`autoWidth` 默认 true

- [ ] **Step 1: 改 `types.ts`**：`widthTiers: number` → `widthTolerance: number`。
- [ ] **Step 2: 改 `registry.ts`**：

```ts
{ key: 'autoWidth', label: '自动变粗', kind: 'toggle', group: '笔宽', default: true,
  hint: '绘制前自动探测画板工具栏并标定笔宽，逐段分配笔宽、按容差合批换笔（默认开启）' },
{ key: 'widthTolerance', label: '换宽粒度', kind: 'slider', group: '笔宽',
  min: 0.02, max: 0.2, step: 0.01, precision: 2, default: 0.05,
  hint: '目标笔宽变化超过该值才换一次笔；越小层次越细、换笔越频繁（0.05 ≈ 最多 20 次）' },
```

（`default` 键名与 DEFAULTS 表结构以该文件现状为准：默认值实际维护在 DEFAULTS 常量里，则把 `widthTolerance: 0.05`、`autoWidth: true` 写进 DEFAULTS，删除 `widthTiers`。）

- [ ] **Step 3: `RunView.vue` 文案**：自动变粗提示追加「程序会先自动识别工具栏控件，个别识别失败时再请到校准页补标」。
- [ ] **Step 4: 构建 + 类型检查** — Run: `cd frontend; npm run build`；Expected: 成功
- [ ] **Step 5: 提交** — `git add frontend/src; git commit -m "feat(frontend): 换宽粒度参数替代笔宽档数，自动变粗默认开启"`

---

### Task 8: E2E 回归与文档收尾

**Files:**
- Modify: `backend/tests/manual/mirror_e2e/`（合成工具栏双态夹具 + 橡皮态开局用例）
- Modify: `docs/open-items.md`（新增 §1A.6 真机项；§0 快照）

**Interfaces:**
- Consumes: Task 6 完成后的完整执行链
- Produces: `run_mirror_e2e.py` 新断言：`switchCount <= ceil(1/tol)+1`、开局橡皮态被纠正为画笔、全程零橡皮点击

- [ ] **Step 1: E2E 夹具扩展** — `mirror_e2e` 的仿真窗口类（文件内 `FakeWindow` 一类命名）增加工具条渲染（复用 Task 3 的 `ToolbarBoard` 逻辑，可 import 或按需复制最小实现，保持夹具自包含），主脚本追加：初始 `tool="eraser"` 运行，断言任务 DONE 且 `task.warning` 序列含「画笔」纠正记录、面板展开点击数 ≤ 21。
- [ ] **Step 2: 跑 E2E** — Run: `cd backend; .venv\Scripts\python.exe tests\manual\mirror_e2e\run_mirror_e2e.py`；Expected: `ALL PASS`
- [ ] **Step 3: open-items** — 新增 §1A.6「真机控件探测与逐段换宽走查（探测命中率、换宽后刻度带抽测宽度 vs 目标、E_NOT_BRUSH 真机触发路径）」；§0 快照加一行「逐段笔宽：实现完成，待真机走查」。
- [ ] **Step 4: 全量回归 + 提交** — Run: `pytest -q` 全绿；`git add backend/tests/manual docs/open-items.md; git commit -m "test(e2e): 镜像夹具覆盖双态工具栏与橡皮态纠正；open-items 登记真机走查项"`

---

## Self-Review 结论

- Spec §1→Task 3；§2→Task 1/2/6；§3→Task 4；§4→Task 4/6/7；§5→Task 1/2；§6→Task 3/5/6（`E_NOT_BRUSH`、基线判定、旧记录回退、展开失败降级）；§7→Task 8；§8 无实现项。
- 类型一致性：`width_buckets` 产出三元组 `(bucket_id, 桶下缘, paths)`，Task 6 循环解包与滑杆点位 `sx = slider.x + slider.w * target_frac` 使用同一语义（下缘=目标）。
- 无占位符；各 fake harness 复用均有精确文件与模式指向。
