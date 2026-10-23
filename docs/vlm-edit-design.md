# VLM 辅助修改绘制结果 扩展设计

状态：里程碑 1 已实现（语义通道全栈 + 版本链与 apply 护栏；生成通道保留协议，未接入时返回 501）。接口编号顺延 design.md 4.1。

## 1. 目标与边界

工作台调参只能全局调整（阈值、模式、密度），无法处理「把 logo 往上移」「头发丝再密一点」「去掉右下角水印」这类局部语义修改。本扩展在预览与绘制之间加入一步对话式编辑：用户用自然语言描述修改意图，视觉模型（VLM）产出新的待绘制图，用户对照确认后进入原有规划流程。

边界约定：

- 编辑产物只是新的「源图版本」，规划、预览、执行链路零改动。
- 每次编辑必须经用户确认才生效，任何模型输出都不直接触发绘制。
- 模型可插拔：本地与云端 provider 同接注册，未发现实现时接口返回 501，与 LineArtProvider 的处理方式一致（design.md 4.1）。
- 云端 provider 把原图传给第三方之前必须有用户显式授权开关，默认关闭。

## 2. 两条编辑通道

用户指令先进入一个轻量解析步骤，按意图分流。两条通道的成本与延迟差一个数量级，分开设计：

| 通道 | 典型指令 | 实现 | 延迟 |
| --- | --- | --- | --- |
| 语义操作 | 平移/缩放/旋转图案、加粗线条、提高密度、裁剪区域、镜像 | 文本模型抽取结构化操作 JSON，本地对掩膜做仿射/形态学变换 | 1–3 s |
| 生成操作 | 增加细节、替换元素、去除物体、改画局部风格 | 图像模型对源图局部（inpaint）或整图重绘，再走现有二值化 | 10–60 s |

结构化操作的 schema 保持最小集合：`{op: translate|scale|rotate|mirror|thicken|thin|crop|erase|density, roi?, dx?, dy?, factor?, angle?, value?}`。解析失败或置信度低时回落为「未理解」提示，不做猜测性执行。

生成操作直接以「指令 + 选区截图 + 上下文」调用图像编辑模型，不强行映射到操作集合。

### 2.1 编辑对象选源图而不是掩膜

通用图像编辑模型是在自然图像上训练的，直接输入二值掩膜会让效果不可控。因此生成通道编辑的对象始终是灰度源图：模型输出仍是源图风格的图，随后复用 `process_image_hq` 二值化得到掩膜，再走 I5 规划。这样做的代价是编辑要经过一次二值化损失，换来的是任何图像模型都能直接接入，且掩膜永远由同一条确定性管线产生——编辑质量与规划质量解耦，问题可归因。

语义通道例外：变换作用在掩膜上（前景即操作对象），结果与预览所见一致，无需再解释二值化差异。

## 3. 交互设计

编辑入口放在工作台预览区上方，与四联缩略图同一层：

1. 选区：在预览图上按住拖拽圈定大致区域（复用 `RectEditor` 的框选交互），不选则视为整图。
2. 指令输入：单行输入框，附常用指令快捷 chips（上移/下移/放大/加粗/去背景杂物）。
3. 生成草稿：语义通道就地应用并即时刷新草稿掩膜；生成通道显示计时遮罩（复用「重新规划中」的样式）。
4. 对照确认：编辑结果以草稿形式呈现，主图左侧原版本、右侧草稿并排，指标条数值同步按草稿重算。只有点「应用」才写回项目。
5. 回退：项目保留源图版本链（见 6），任意历史版本可一键切回，编辑失败或效果不满意时可完全还原。

编辑后的流程与重新导入等价：应用即产生新源图版本，自动触发一次规划，目标掩膜/模拟笔迹/差异图照常更新。墨量告警、工作倍率降档等既有防护对新版本同样生效。

## 4. 接口设计

| 编号 | 接口 | 说明 |
| --- | --- | --- |
| I16 | `GET /api/edit/providers` | 已注册的编辑 provider 列表（`[{id, label, kind: semantic|generative|both, local}]`），空数组表示未配置 |
| I17 | `POST /api/projects/{projectId}/edits` | 发起编辑，返回草稿；请求见下 |
| I18 | `POST /api/edits/{editId}/apply` | 应用草稿，产生新源图版本，响应含新项目快照与新 plan 所需参数 |
| I19 | `DELETE /api/edits/{editId}` | 丢弃草稿 |

I17 请求体（camelCase，与既有 schema 风格一致）：

```json
{
  "instruction": "把头发改得更密一些",
  "roi": {"x": 120, "y": 40, "w": 300, "h": 220},
  "provider": "vlm-local",
  "baseVersion": 3
}
```

响应 `201`：

```json
{
  "editId": "…",
  "channel": "generative",
  "parsedOps": null,
  "draftUrl": "/api/edits/…/draft",
  "diffUrl": "/api/edits/…/diff",
  "appliedAt": null,
  "createdAt": "…"
}
```

语义通道 `parsedOps` 回传结构化操作供前端展示「模型理解成了什么」；无法解析时 `422 E_EDIT_NOT_UNDERSTOOD`，前端提示改写指令，这条错误码需要加入 design.md 4.4 错误码表。

## 5. Provider 协议与注册

与 `LineArtProvider` 同风格，定义在 `app/services/editing.py` 暴露的协议：

```python
class EditProvider(Protocol):
    id: str
    label: str
    supports: Literal["semantic", "generative", "both"]

    def parse(self, instruction: str) -> list[EditOp]: ...        # 语义通道
    def apply(self, image: Path, ops: list[EditOp]) -> Path: ...  # 语义通道，本地确定性执行
    def generate(self, image: Path, roi: Rect | None,
                 instruction: str) -> Path: ...                   # 生成通道，模型调用
```

注册方式沿用 design.md 的 entry point 方案：第三方包声明 `cupdraw.edit_providers` entry point，服务启动时发现。内置一个纯本地语义 provider（OpenCV 实现 ops 执行，解析步骤可选接任意文本模型，未配置文本模型时用关键词规则兜底），保证不装任何扩展时语义通道开箱可用、生成通道返回 501。

云端生成 provider（GPT-4o image、Gemini、通义万相等）作为独立插件包发布，密钥从各自环境变量读取，本项目不落盘任何密钥。

## 6. 数据与存储

编辑草稿与版本链沿用五实体模型做最小扩展：

- `project` 表增加 `source_version INTEGER DEFAULT 1`、`parent_version` 溯源。
- 上传目录内源图命名为 `source_v{n}.png`，I3 行为不变（新版本即 v1）。
- 新表 `edit_draft(id, project_id, base_version, channel, instruction, roi_json, provider, draft_path, diff_path, created_at, applied_at)`，草稿图片存 `data/edits/{editId}/`。
- apply 事务：写入 `source_v{n+1}.png` → 更新 project 指向 → 草稿标记 applied。失败则整体回滚，旧版本不受影响。

磁盘回收：每项目保留最近 5 个版本与全部已应用版本，多余的旧草稿按 TTL（7 天）清理，规则沿用 design.md 5.2 的资产目录约定。

## 7. 安全与兜底

- 人工确认是唯一生效路径，接口层面草稿与正式资产隔离在不同目录，绘制与规划永远只读 `source_v{n}.png`。
- 生成结果应用前跑一次既有墨量检测：编辑后墨迹占比 > 40% 或 < 1% 时阻断应用并提示（复用 5.1 inkRatio 指标）。
- 云端上传授权：Settings 增加 `allow_cloud_edit: bool = False`，为 False 时 I17 指定云端 provider 返回 `403 E_CONSENT_REQUIRED`。
- 模型输出尺寸与源图不符时本地拉齐（contain 补白），拉齐失败按编辑失败处理。
- 编辑接口复用会话门禁与单进程池超时策略；生成通道超时上限独立配置（默认 180 s，规划池不受影响，编辑走独立单线程队列）。

## 8. 测试与验收

- 契约测试：I16 空列表 / I17 两通道 happy path 与未理解 / I18 apply 后版本链正确 / I19 丢弃后草稿不可访问 / 未授权 403 / 无 provider 501。
- 语义通道金标准：固定样例图上「上移 50px」「镜像」「加粗 2px」三组指令，断言变换后掩膜与手工 OpenCV 结果 IoU ≥ 0.99。
- 生成通道只测管线：注入 Fake provider 返回固定图片，断言二值化、版本落盘、diff 生成、apply/回退全链路正确；模型效果用人工验收清单在真机走一遍（水印移除、局部加密、元素移动三类指令）。
- 前端走查：框选→指令→草稿对照→应用→自动重规划→绘制全链路，无控制台错误。

## 9. 里程碑切分

1. 语义通道 MVP：解析（规则兜底）+ 本地 ops 执行 + 草稿/apply 接口 + 前端编辑条与对照视图。价值最直接，无任何外部依赖。
2. 版本链与回退 UI、磁盘回收。
3. 生成通道协议 + 一个本地图像模型 provider（如 SD inpaint）+ 授权开关。
4. 云端 provider 插件与效果调优（提示词模板、选区外像素回填保护）。

先生成语义通道即可覆盖日常高频修改（移位、缩放、去杂物、调粗细），生成通道处理真正需要画图的场景，两级投入可按验收反馈独立推进。
