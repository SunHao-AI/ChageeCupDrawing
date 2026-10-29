# 清空画布与校准引导 增量设计方案

## 文档元信息

| 维度 | 判定 |
| --- | --- |
| 文档类型 | HLD 增量设计（对 `undo-rollback-design.md` 回退能力与工具栏探测/校准链路的扩展） |
| 系统类型 | Brownfield（复用回退一笔、工具条探测、面板开合、墨量测量等既有积木） |
| 主要受众 | 后端工程师、前端工程师、设计评审 |
| 复杂度 | Medium |
| 上游输入 | `design.md` + `undo-rollback-design.md` + 用户反馈（清空诉求、探测未触发、引导只有文字、空白判定应基于参考帧） |
| 明确不覆盖 | 撤销深度扩展、多画布/多窗口清空、橡皮探测算法改动 |

本文只覆盖增量部分。凡本文未提及的模块、接口、数据结构，语义与既有文档完全一致；冲突处以本文为准。

---

## 1 设计总览

### 1.1 问题与方案

用户反馈四件事，各对应一个缺口。

**画布上的旧墨没有出路。** 回退一笔是逐笔的，撤十几行测试线或半幅废画不现实；重新导入新图后小程序画板上的旧线稿还在，新画会叠上去。需要一个「清空画布」：以撤销循环为主（连点目标自己的撤销按钮直到无墨），撤销历史见底仍有残留时以橡皮兜底（切最大笔宽整行扫涂）。用户点「清空」即表示画布上的墨均可弃，这是与回退一笔（逐笔确认）不同的授权边界。

**「画布是否有墨」的判定过脆。** 现状是绝对判据：暗于固定阈值 160 的像素超过 64 个即判有墨。画布带水印/纸张纹理/非白底时干净画布会被误判"有墨"（跳过探测、拒绝清空短路）；投屏压缩变淡时浅墨迹又会漏判。改为用户建议的参考帧思路——空白画布快照作为基线、当前画布与之逐像素比较——并以自适应背景阈值兜底，两层合一见 §3。

**工具条自动探测存在不触发的死角。** 现状触发条件是 `自动变粗开启 && 首笔 && 从未探测 && 画布空白`。关掉自动变粗就永远不探测——可撤销/折叠/画笔坐标的价值独立于笔宽；画布有墨就跳过探测——而画布有墨恰恰是"想重画"的常态场景，且清空又需要撤销坐标、撤销坐标又来自探测，形成闭环死锁。方案：探测与自动变粗解耦（首绘且无双态位图即探测）；空白门禁保留，但命中时改为推送结构化提示码，前端展示可操作的引导卡——用户确认「画布墨迹可弃」后，允许在有墨状态下探测（盲点候选已获得授权），随后即可清空、标定、从头开画。

**校准引导只有文字。** 校准页四个框选模式各配一张示意图（内联 SVG 画板工具条布局 + 高亮目标控件）；运行页的探测跳过/失败警告升级为带按钮的引导卡（清空画布 / 去校准页对应模式）。

换图沿用校准：重新上传图片走的是新建项目，校准全部丢失、需从头框选。校准挂在窗口目标上而与图片内容无关，新项目的创建请求可携带来源项目 id，后端把该项目最新一条校准记录整体复制过来（画布矩形、滑杆、撤销、折叠、双态位图全部保留），落笔前既有漂移校验会兜住窗口已移动的情况。

### 1.2 需求追溯

| 编号 | 需求 | 来源 | 设计承载 |
| --- | --- | --- | --- |
| R18 | 一键清空小程序画布上的绘制内容 | 用户新增 | 统一空白判定（§3）+ 清空画布（§4） |
| R19 | 用户想从头开始（重新上传图片）时代价最小 | 用户新增 | 换图沿用校准（§5）+ 清空后从头开画（§4.5） |
| R20 | 笔宽/撤销等控件校准应自动触发；手动校准时给图文引导 | 用户新增 | 探测触发解耦（§6）+ 引导升级（§7） |
| R21 | 画布空白判定应基于空白参考帧比较，而非固定阈值 | 用户新增（本次） | 统一空白判定（§3） |
| R16' | 误触墨迹可逐笔回退（继承） | `undo-rollback-design.md` | 不改动；清空是其批量形态，共用撤销坐标与面板开合 |
| R13' | 探测点击绝不落画布内（继承） | 既有约束 | 全部保留；橡皮扫涂是唯一的画布内动作，且只在橡皮态校验通过后执行 |

### 1.3 设计目标与约束

| 类型 | 内容 |
| --- | --- |
| 目标 | 清空 20 笔以内的画布 ≤ 15 s（每次撤销约 0.5 s：点击 + 沉降 + 截图复核）`[Expert judgment]` |
| 目标 | 撤销到底仍有残留时橡皮兜底的清空率 ≥ 99%（最大笔宽行扫涂、行距留 50% 重叠）`[Expert judgment]` |
| 目标 | 带水印/纹理背景的画布上，空白判定零误报（基线帧包含全部常驻背景特征）`[Expert judgment]` |
| 目标 | 换图后从导入到可开画的步骤 = 导入 → 开画（校准沿用，漂移时仅需重跑一次自动识别）`[Expert judgment]` |
| 约束 | 清空流程全部复用 `ink_measure` 语义扩展，不引入独立视觉管线 `[Expert judgment]` |
| 约束 | 橡皮兜底前必须确认当前是橡皮态且笔宽已置最大档；扫涂结束必须切回画笔态，校验失败要在结果消息中显式暴露 `[Expert judgment]` |
| 约束 | 有墨状态下的探测（脏画布探测）只在用户显式确认后执行，探测自身的清理逻辑（撤到基线为止）不因脏画布放宽 `[Expert judgment]` |
| 约束 | 沿用校准只复制坐标类字段；`source` 标记为 `copied`，校准页照常展示、照常可改 |
| 约束 | 空白判定的基线学习全程无需用户动作；绝对墨量始终作为兜底下界，防止基线自身学脏导致清空永无出头 |

---

## 2 已有积木

| 积木 | 位置 | 本设计中的用途 |
| --- | --- | --- |
| 撤销一次并验证墨量下降 | `ExecutorService._undo_with_handle` | 清空 = 同一坐标连点 N 次；抽出点击内核复用 |
| 面板开合（滑杆轨道可见性判态） | `executor/panel_control.py` | 清空前展开、结束后折回 |
| 双态工具位图（brush/eraser/slider/undo/fold + 画笔选中指纹） | `CanvasProfile.tools_open` | 橡皮兜底的坐标与工具态校验 |
| 墨量测量 | `ink_measure.ink_pixels / ink_occupied_fraction` | 清空进度与「撤到底」判定 |
| 画布整体变化检测（翻转 >50%） | `ink_measure.detect_stray_ink` | 空白基线失效判定复用同一翻转判据 |
| 画布矩形检测与坐标平移 | `detect_canvas_rect` + `dx,dy` 平移 | 清空时的当前帧坐标换算 |
| 滑杆档位点击（两端内缩） | `PenCalibrator.TRACK_EDGE_INSET` 语义 | 橡皮兜底把笔宽置到最粗档 |
| 结构化警告通道 | SSE `task.warning` | 增加 `code` 字段驱动前端引导卡 |

---

## 3 统一空白判定（canvas_blankness）

### 3.1 现状与问题

现有判据是绝对量：画布区域内 `灰度 < INK_THRESHOLD(160)` 的像素数 `≥ PROBE_BLANK_MIN_PIXELS(64)` 即判"有墨"（`_canvas_is_blank`、`undo_once` 空画布短路、`PenCalibrator._clear_canvas` 基线判定各自为政）。两个失效方向：

| 场景 | 后果 |
| --- | --- |
| 画布带水印、纸张纹理、深色装饰、杯贴底图透出 | 干净画布被误判有墨 → 跳过探测、清空不短路、回退不短路 |
| 投屏压缩/低码率使浅墨迹灰度高于 160 | 有墨被误判空白 → 探测盲点风险、清空提前报成功 |

用户建议"自动截取空画布作参考、与当前画布比较"。方向正确——常驻背景特征进基线后不再干扰，笔画相对基线永远是新增像素；难点只有一个：**第一张空白帧从哪来**（画布可能一开始就有墨）。因此不单独依赖参考帧，做两层，参考帧为主、自适应阈值兜底。

### 3.2 L1 自适应背景阈值（免参考帧）

```python
def estimate_background(gray_crop) -> int:
    return int(np.percentile(gray_crop, 90))   # 背景占画布绝大多数面积，P90 稳定落在背景

def blank_ink_pixels(image, region, *, delta=60) -> int:
    """region 内相对自适应背景的墨像素数：pixel < background - delta。"""
```

白底画布 `background≈255`，等价于现行为（255−60=195 与 160 同量级、更宽容投屏噪点）；带底色画布自动跟随。`delta=60` 远小于任何可辨认笔迹与背景的对比度 `[Expert judgment]`。L1 是 `_canvas_is_blank` 的直接升级，改动局部。

### 3.3 L2 空白基线帧（自学习参考）

`CanvasProfile` 新增 `blank_baseline` 字段（JSON：`{"w":…, "h":…, "mask": base64}`，墨迹布尔掩码按 1/8 下采样；投屏画布 800×1000 → 100×125 bool ≈ 1.6 KB）。

**学习时机（全自动，无用户动作）：**

| 时机 | 动作 |
| --- | --- |
| 首次 L1 判空白且 profile 无基线 | 快照当前帧掩码 → 存为基线 |
| 清空画布成功（复查空白）但基线 diff 超阈 | 重学基线（背景可能变了：主题切换、底图更换） |
| 当前帧与基线整幅翻转占比 >50%（`detect_stray_ink` 同一判据） | 基线作废置 None，等待下次 L1 重学 |

**判据：**

```python
def is_blank(image, region, baseline) -> bool:
    if baseline is None:
        return blank_ink_pixels(image, region) < BLANK_MIN_PIXELS
    added = 当前墨掩码(自适应背景) & ~baseline掩码(对齐到当前 region 帧)
    return added_pixel_count < BLANK_MIN_PIXELS
```

基线以"基线采样时的画布矩形"为帧；比较前按 `detect_canvas_rect` 的当前矩形做比例对齐（画布缩放时掩码 resize），对齐后残余错位由 1/8 下采样与 `BLANK_MIN_PIXELS` 容差吸收 `[Expert judgment]`。

### 3.4 绝对量兜底与调用点统一

基线有一个恶性学习路径：L1 误判脏画布为空白 → 基线学成了"脏的" → 之后永远判空白 → 清空循环看不到出头之日。兜底：**任何"判空白"结论还须满足 `ink_pixels(image, region) < BLANK_ABS_CEILING`（= `PROBE_BLANK_MIN_PIXELS × 4` = 256 px）**——真空白画布绝无 256 个绝对墨像素，而学脏的基线救不回的画面会被绝对量拦住并触发基线作废重学。

替换调用点：

| 调用点 | 现状 | 改为 |
| --- | --- | --- |
| 探测前空白门禁 `_canvas_is_blank` | 固定 160 / 64 px | `is_blank`（L2 优先，L1 兜底，绝对量封顶） |
| 回退一笔空画布短路 | `ink_pixels < 64` | `is_blank` |
| 清空完成复查（§4.2 步骤 9） | `ink_pixels < 64` | `is_blank` |
| 清空循环 plateau 判定 | `ink_pixels` 绝对 Δ | 不变（它是进度增量，不是空白判定） |

判定与学习都在执行器侧封装为一个辅助（`CanvasBlankness`：判 + 学习 + 存 profile），`ink_measure` 只提供 `blank_ink_pixels` 纯函数，保持"纯函数在 ink_measure、状态机在 service"的既有分层。

---

## 4 清空画布

### 4.1 接口

`POST /api/projects/{projectId}/clear-canvas`（I19，项目级：清空的对象是目标画布，与任务无关；无任务时也可用）。

请求体（可省略）：

```json
{ "confirmDirtyProbe": false }
```

成功 200：

```json
{
  "cleared": true,
  "method": "undo | undo+eraser | already_blank",
  "inkBefore": 48213,
  "inkAfter": 0,
  "message": "已清空画布（撤销 23 次）"
}
```

错误语义：

| 状态 | 错误码 | 触发条件 | 前端动作 |
| --- | --- | --- | --- |
| 422 | `E_CANVAS_MISSING` | 项目无校准 | 引导去校准页 |
| 409 | `E_TASK_CONFLICT` | 执行槽位被活动任务占用 | 提示先停止绘制 |
| 422 | `E_CLEAR_UNAVAILABLE` | 无撤销坐标且无橡皮坐标，且画布非空白 | 弹二次确认 → 带 `confirmDirtyProbe: true` 重发 |
| 409 | `E_CLEAR_FAILED` | 撤销+橡皮后仍非空白 / 截图失败 / 面板无法展开 | 展示 message，建议手动清空画板 |

`confirmDirtyProbe` 的语义是「我确认画布上的墨迹可以丢弃，允许程序在画布有墨时对画布下方工具条区域进行探测点击」。第二次请求若仍缺撤销坐标，执行器先跑一次带脏画布授权的探测（§6.2），成功后继续清空。

### 4.2 主流程（`ExecutorService.clear_canvas`）

```text
1. 取项目最新校准 → 无校准 422
2. resolve_target → _claim 槽位（与 start/resume/undo 互斥）
3. 前台确认 → 截图 → detect_canvas_rect → dx,dy 平移校准坐标
4. is_blank(image, canvas, baseline)?
   是 → 返回 already_blank（不点任何东西）；顺带按 §3.3 规则学习/刷新基线
5. 撤销坐标缺失 → 按 §4.1 错误表分流（confirmDirtyProbe=true 时先脏探测再回来）
6. 面板展开（有 slider+fold 时；展开失败 409 E_CLEAR_FAILED）
   展开后重取当前画布矩形做后续点击守卫
7. 撤销循环（§4.3）
8. 仍非空白且有橡皮坐标+滑杆坐标 → 橡皮兜底（§4.4）
9. 复查 is_blank：仍非空白 → 409 E_CLEAR_FAILED
   已空白且基线 diff 超阈 → 重学基线（§3.3）
10. finally：切回画笔态（若切过橡皮）→ 折回面板 → mouse.up → _release
```

同步 HTTP 执行，与 `undo_once` 同一模式（不进 SSE）。全程 ≤ 60 s，超时保护由撤销循环预算承担。

### 4.3 撤销循环

复用 `PenCalibrator._clear_canvas` 的循环形态，判据换成空白判定（§3）：

```text
budget = CLEAR_UNDO_MAX_CLICKS (默认 80)
plateau_clicks = 0
for _ in range(budget):
    if stop_event: 中止 → E_CLEAR_FAILED（已清多少算多少，如实回报）
    点击撤销 → settle 0.22 s → 截图 → ink = ink_pixels(canvas)
    is_blank(当前帧) → 成功返回
    |ink - 上次ink| < UNDO_EFFECT_MIN_PIXELS → plateau_clicks += 1
    plateau_clicks ≥ CLEAR_PLATEAU_CLICKS (默认 3) → 撤到底，break 进橡皮兜底
```

连续 3 次墨量不动才判「撤到底」，是为了容忍小程序撤销动画的截图中途态；每次点击都截图复核，撤错的每一笔都在墨量数字里如实呈现。

### 4.4 橡皮兜底

前提：`tools_open.eraser` 与 `tools_open.slider` 均已知。缺任何一项则跳过兜底，最终复查不过即 `E_CLEAR_FAILED`（message 提示撤销深度不足，请手动清空画板）。

```text
1. 面板保持展开（撤销循环已在展开态）
2. 笔宽置最粗档：点击滑杆轨道右端（内缩 TRACK_EDGE_INSET，与 PenCalibrator 同语义）
3. 点击 eraser → 校验离开画笔态（有完整选中指纹时）；指纹缺失则盲信坐标
4. 行扫涂：在当前画布矩形内，自顶向下逐行「左→右 / 右→左」交替划线
   行距 = max(width_table 最粗档, 28) × 0.5（50% 垂直重叠）
   首末行压住画布上下缘内 4px；左右各超出画布 8px（超出部分在窗口内、画布外，无害）
5. 扫完截图复查：仍非空白 → 再扫一轮（最多 2 轮）
6. 点击 brush 切回画笔态；选中指纹校验失败时在返回 message 中警告「请目视确认已切回画笔」
```

扫涂全程走 `_stroke` 注入路径（mouse down/move_sample/up），落在画布内是预期行为——这是本设计唯一允许在画布内画线的流程，且只在橡皮态下执行。行数 = 画布高 / 行距，投屏画布典型 800 px 高约 15–20 行，两轮回重试上限内耗时可控。

风险与缓解：橡皮坐标错位（点到撤销等别的按钮）→ 扫涂第一行后立刻复查墨量，墨量不减反增（画上了墨）立即中止并 `E_CLEAR_FAILED`，不继续画脏。

### 4.5 「清空并重新绘制」前端编排

RunView 增加组合动作（STOPPED / DONE / FAILED 且已校准时可见）：

```text
点「清空画布，从头绘制」→ 确认 GuideCard（明示旧墨全部丢弃）
→ POST clear-canvas（失败带 E_CLEAR_UNAVAILABLE 时升级到脏画布探测二次确认）
→ 清空成功 → 调既有 start()（新建任务天然从第 0 笔开始）
```

不改任务状态机、不加 ABANDONED 状态：`start()` 的后端冲突判定只挡活动任务，STOPPED 任务留在库里作历史。若 `start()` 现状对 STOPPED 有拦截，实现时放开为「STOPPED 任务允许另起新任务，旧任务保持 STOPPED」——语义与刷新页面后重新点开始完全一致。

---

## 5 换图沿用校准

### 5.1 机制

`POST /api/projects`（multipart）增加可选表单字段 `copyCalibrationFrom=<projectId>`。创建成功后、返回前，后端把来源项目的 `latest_calibration` 整体复制到新项目（`store.save_calibration` 逐字段搬运，含 `blank_baseline`，`source="copied"`）。坐标天然同帧——两次导入之间目标窗口未动就是零成本；动过了则落笔前漂移校验拦截，或用户在校准页点一次「自动识别」刷新画布矩形（现有流程）。

前端：`projectStore.createProject` 在发起前捕获当前 `project?.projectId`（非空即传 `copyCalibrationFrom`）。刷新恢复路径（`restore()` + `lastProjectId`）已保证页面重开后 store 里有"上一项目"，行为一致。

### 5.2 边界

| 场景 | 行为 |
| --- | --- |
| 来源项目无任何校准 | 不复制，新项目与现状相同 |
| 复制后目标窗口换了（PC↔投屏） | 校准页现有 `deviceKind` watcher 自动重新识别，覆盖复制来的记录（现有行为，不需要改） |
| 用户明确想要全新校准 | 校准页「自动识别」一次即可覆盖 |

---

## 6 探测触发解耦

### 6.1 触发条件变更

```text
旧: params.auto_width and cursor == 0 and calibration.tools_open is None and 画布空白
新: cursor == 0 and calibration.tools_open is None
     ├─ is_blank → 照常自动探测
     └─ 非空白 → 跳过，推送 warning {code: "probe_skipped_dirty", ...}
```

自动变粗仍只控制「标定笔宽表」这一段；探测（撤销/折叠/橡皮/画笔坐标）在任何模式下首绘都跑。`auto_width=false` 时探测成功后不再进入 `_run_pen_calibration`，其余不变。

### 6.2 脏画布探测授权

`ToolbarProber` 本身不感知画布是否有墨，门禁在执行器侧。新增内部入口 `clear_canvas(confirm_dirty_probe=True)` 的分支：先带现有 `manual` 兜底跑一次 `_probe_toolbar`（脏画布下允许——用户已授权），成功后撤销坐标入库，继续 §4.2 的清空。任务路径（`_arm_and_draw`）不做脏探测：绘制任务的默认语义不含"丢弃现有画作"，用户想要就走清空引导。

warning 载荷扩展：

```json
{ "message": "画布上已有笔迹，已跳过工具条自动探测…", "code": "probe_skipped_dirty" }
```

`code` 为可选字段，老客户端忽略即可；SSE 事件结构不变，`TaskWarningPayload` 前端类型加 `code?: string`。同类码还有 `probe_failed`（探测失败回退旧坐标，来自现有 warning 的语义化）。

---

## 7 引导升级（前端）

### 7.1 校准页示意图

CalibrateView 四个框选模式（画布/滑杆/撤销/折叠）各配一张内联 SVG 示意图：小程序画板窗口的简化布局（画布区 + 底部工具条 + 笔宽滑杆/撤销/折叠按钮），当前目标控件高亮金色、其余灰显，一句话说明 + 一个箭头标注点击/框选位置。SVG 作为组件内联（新增 `CalibDiagram.vue`，按 mode 渲染），不用位图——高亮可随模式即时切换，风格随主题变量走。

### 7.2 运行页引导卡

| 触发 | 展示 | 动作按钮 |
| --- | --- | --- |
| warning `code=probe_skipped_dirty` | GuideCard：画布有旧墨迹，跳过自动学习 | 「清空画布，从头绘制」（§4.5 全流程）/「去校准页」 |
| warning `code=probe_failed` | GuideCard：自动学习未完成 | 「去校准页补标」（直达校准页，`calibTarget='slider'`）|
| undo 422 `E_UNDO_UNAVAILABLE` | 现有 undoFeedback banner 补按钮 | 「去校准页标撤销」（`calibTarget='undo'`） |

`goto` 事件现有签名只带视图名；增加一次性提示状态（`sessionStorage` 或共享 ref）让校准页打开时预选对应 `calibTarget`，实现从简用 store 字段传递即可。

### 7.3 运行页文案

清空/回退相关引导统一话术：「画布上的墨迹将被全部丢弃」出现在所有不可逆动作的确认面上，与回退一笔的「逐笔确认」形成明确对比。

---

## 8 接口与数据汇总

| 变更 | 类型 | 说明 |
| --- | --- | --- |
| `POST /projects/{id}/clear-canvas` | 新接口 I19 | 同步清空画布，body 可带 `confirmDirtyProbe` |
| `POST /projects` | 扩展 | multipart 可选 `copyCalibrationFrom` |
| SSE `task.warning` | 扩展 | 载荷增加可选 `code` 字段（`probe_skipped_dirty` / `probe_failed`） |
| `CanvasProfile.blank_baseline` | 新列（小迁移） | 空白基线掩码 JSON（下采样 + base64），可空 |
| `TaskWarningPayload`（前端） | 扩展 | `code?: string` |
| 任务状态机 / 游标 / ExecParams | 不变 | 清空不改任何任务记录 |

---

## 9 实施顺序与测试

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| B0 | 统一空白判定：`blank_ink_pixels` 纯函数 + `CanvasBlankness`（L1/L2/绝对兜底）+ 三个调用点替换 + 基线列迁移 | 单测：白底/纹理底/学脏基线/整幅翻转作废四类用例；既有墨量测试全绿 |
| C0 | 清空画布（撤销循环）+ I19 + RunView「清空并从头绘制」编排 | 契约测试：Fake 画布带墨 → clear → 空白、面板折回、任务不受影响；撤到底 plateau → E_CLEAR_FAILED |
| C1 | 探测解耦 + warning `code` + 脏画布探测授权分支 | 单测：autoWidth=false 也探测；脏画布 warning 带码；confirmDirtyProbe 二次请求先探测后清空 |
| C2 | 橡皮兜底（§4.4） | 单测：撤销 plateau → 橡皮扫涂 → 空白、切回画笔；扫涂墨量回升即中止 |
| C3 | 换图沿用校准 | 契约测试：带 copyCalibrationFrom 创建 → latest_calibration 与来源一致且 source=copied |
| C4 | 校准页 SVG 示意图 + 运行页引导卡 | 手动走查四模式高亮与跳转预选 |

回归门槛：既有全套 + 前端 `vue-tsc`。真机实测项（实现后确认）：小程序撤销历史深度对 C0/C2 分流比例的影响；投屏延迟下 plateau 判定（0.22 s + 截图）是否会误判撤到底；`delta=60` 对实际投屏码率的适配性。

## 10 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 小程序撤销深度 < 画布笔数 | 撤销撤不干净 | plateau 自动转橡皮兜底；两者都失败时 message 明示手动清空 |
| 橡皮坐标错位把画布画脏 | 越清越脏 | 扫涂第一行后复查墨量方向，不减反增立即中止 |
| 基线学到脏画布（L1 误判连锁） | 永远判空白、清空无出头 | 绝对墨量封顶 `BLANK_ABS_CEILING` 兜底 + 触发基线作废重学 |
| 画布缩放导致基线掩码错位 | 空白误报/漏报 | 掩码按当前矩形 resize 对齐 + 1/8 下采样容差 + 绝对量封顶 |
| 脏画布探测盲点误触「清空类」以外的破坏性控件 | 用户已授权丢弃墨迹，损害被授权覆盖 | 确认文案明示"程序将点击画布下方工具条区域" |
| 沿用校准的窗口已失效（关窗/换设备） | 落笔失败 | 既有 alive/漂移校验兜底，报错文案引导重新识别 |
