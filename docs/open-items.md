# 未完成事项清单

> 生成时间：2026-10-24（§1.2 验收脚本 BYOK 适配完成后更新）  
> 用途：本会话收尾盘点，供新会话按条处理。每条包含「描述 / 背景 / 要求 / 完成判据 / 关联文件」。  
> 本轮（输入主权守护 + 多设备）新增项集中在 **§1A**，已交付项见 §6。

## 0. 当前状态快照

| 项目 | 状态 |
| --- | --- |
| 后端测试 | `pytest` 全量 **609 项通过**（含守护/设备契约测试，2026-10-23 B2 完成后实测；上一记录为 581） |
| 后端服务 | 运行中，`127.0.0.1:8765`，`/api/health` = 200（已加载 B2 镜像目标代码） |
| HTTPS 反代 | 已按要求停止并删除 `deploy/`（2026-10-24），开发直连 `http://127.0.0.1:8765` |
| 前端构建 | `frontend/dist` 已构建（含 BYOK 改造、响应式断点体系、iPad 字号修复） |
| 无手机全链路 | `tests/manual/mirror_e2e/run_mirror_e2e.py` ALL PASS（注册→校准→规划→绘制→守护 62ms 停笔→断点续画 2/6→DONE） |
| 开机自启 | 已按用户要求移除（开发机不做运维配置） |
| 代码提交 | 本轮全部改动已提交（2026-10-24，未推送远端），见 §3 |

---

## 1. 功能验收类（阻塞于外部条件）

### 1.1 M-C 提示词人工验收未通过 ❗高

- **描述**：9 例真实模型验收（加字 3 例、微调 3 例、删元素 3 例）尚未取得有效结果，当前进度 0/9。
- **背景**：上次跑批 `run-20261023-134941` 全部失败，原因是 OpenAI 账户余额耗尽返回 `429 credit_balance_exhausted`，与提示词质量无关。判据为「每类 ≥2/3 通过视作达标，不达标只收敛提示词模板、不动架构」。
- **要求**：提供可用 Key（OpenAI 充值后，或改用 Gemini / DashScope），重跑 9 例并产出对照报告。脚本已适配 BYOK（见 §1.2），拿到 Key 后直接 `$env:CUPDRAW_AI_KEY="…"; python .benchmarks/mc_acceptance.py --provider <id>` 即可。
- **完成判据**：`backend/data/mc_acceptance/run-<时间戳>/report.html` 生成，9 例均出图，三类通过率有结论。
- **关联**：[mc_acceptance.py](file:///d:/Workplace/ChageeCupDrawing/backend/.benchmarks/mc_acceptance.py)、[ai-edit-provider-design.md](file:///d:/Workplace/ChageeCupDrawing/docs/ai-edit-provider-design.md)（§7 M-C）

### 1.2 验收脚本需适配 BYOK（Key 不再落盘）✅已完成（2026-10-24）

- **描述**：`mc_acceptance.py` 用 `AiSettingsStore.load()` 取 Key，而 BYOK 改造后磁盘上的 Key 已在启动时被 `purge_stored_key()` 清除，脚本必须依赖环境变量才能拿到 Key。
- **落地情况**：脚本已加 `--provider`（gemini-image / openai-compatible / dashscope 三选一）、`--model`、`--base-url`、`--key-env` 参数；Key 只从环境变量读取（默认 `CUPDRAW_AI_KEY`，绝不走命令行），缺失时打印带 PowerShell 示例的明确提示并以 2 退出。`--provider` 会同时固定 `route()` 预检与逐例 `create_draft` 的通道，不再自动漂移。已用假 HTTP 桩验证 `--provider dashscope` 9/9 跑通、`report.html`/`run.json` 完整。
- **关联**：同上；[settings_store.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/services/settings_store.py)

### 1.3 提示词模板调优（依赖 1.1 结果）

- **描述**：若 1.1 中有类别未达 2/3，需要收敛提示词模板（输出白底线稿、保持原线条风格、不新增底纹）。
- **背景**：出图后还要过二值化/素描化，模型自由发挥画风会让二值化不可控；墨量护栏（1%–40%）只兜底不治本。
- **要求**：仅改模板与护栏阈值，不改管线架构。
- **完成判据**：重跑后三类均 ≥2/3。
- **关联**：[roi_pipeline.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/services/roi_pipeline.py)（`build_prompt`）

---

## 1A. 真机走查类（本轮新增，阻塞于真实设备）

### 1A.1 微信小程序窗口的守护不误停验证 ❗高

- **描述**：在真实「霸王茶姬」小程序窗口（微信内嵌子窗口）上实测输入主权守护，确认既不迟停也不误停。
- **背景**：守护逻辑已实现（[guard.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/executor/guard.py)），且已在普通 Chrome 窗口上用无手机 E2E 坐实「最小化 → 62 ms 抬笔、`stopReason=focus_lost`」。但微信小程序的窗口层级是「外层 WeChat 主窗 + 内嵌渲染子窗」，前台判定沿 `GA_ROOTOWNER` 上溯归一比较（[windows.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/device/windows.py) 的 `foreground_matches`），这条归一路径**只在单测里验过**，未在真实微信窗口上回归。若归一失效会表现为「用户没动任何东西却反复 focus_lost 中断」。
- **要求**：真机打开小程序画板 → 校准 → 开始绘制，依次执行：① Alt+Tab 切走；② 抢鼠标移动 >6 px；③ 物理左键点击；④ 最小化小程序；⑤ 完全不操作作为对照（须能画完不中断）。每次都在 100 ms 内抬笔，且对照场景零误停。
- **完成判据**：①–④ 均 `STOPPED` 且 `stopReason` 分别为 `focus_lost`/`mouse_takeover`/`mouse_takeover`/`focus_lost`；⑤ 正常 DONE。
- **关联**：[focus-guard-and-multidevice-design.md](file:///d:/Workplace/ChageeCupDrawing/docs/focus-guard-and-multidevice-design.md) §3

### 1A.2 真机 scrcpy 投屏目标联调 ❗高

- **描述**：安卓真机经 scrcpy 投屏后完成校准→绘制→守护全流程；重点确认 GPU 合成窗口能否被截图抓到。
- **背景**：B2 已实现「投屏窗口 = 又一个 Win32 目标」（[mirror.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/device/mirror.py)、[target.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/device/target.py) 的 `MirrorTarget`），并用 Chrome 窗口跑通全链路。**唯一未验证的假设**：scrcpy 窗口是 SDL/GPU 合成渲染，设计假定 MaaFramework 的 Windows.Graphics.Capture 截图能抓到画面。
- **要求**：安卓机开 USB 调试并授权，安装 scrcpy（官方 release 自带 adb），或用 `CUPDRAW_SCRCPY` / `CUPDRAW_ADB` 指定路径。校准页出现「投屏 xxx」后一键投屏。若截图为黑屏，按既定退路：截图改走 `adb exec-out screencap -p`，注入仍走投屏窗口（`DrawingTarget` 接口不变，仅拆 `MirrorTarget` 的 capture 实现）。
- **完成判据**：真机画板完成一次含守护中断与续画的完整绘制；逐点时延有实测数据（决定是否推进 B3 ADB 直连）。
- **关联**：设计文档 §4.5–§4.6

### 1A.3 scrcpy 随包分发的许可与体积未确认 ⚠中

- **描述**：若最终要随应用分发 scrcpy + adb 二进制，需确认许可合规与体积影响。
- **背景**：scrcpy 为 Apache-2.0，其依赖（ffmpeg、SDL、adb）许可各异；完整 Windows 包解压约 40–90 MB。当前实现是「探测 PATH / 环境变量，缺失则报 `E_SCRCPY_MISSING` 并在前端给安装指引」，不随包分发。
- **要求**：产品决策——维持「用户自装」（现状）还是随包分发。若随包，需附第三方许可清单并处理升级路径。
- **完成判据**：决策记录进设计文档，现状或方案二选一落地。

### 1A.4 真机 iPad 字号验证（两种 Safari 浏览模式）⚠中

- **描述**：真机 iPad 上分别以「请求桌面网站」开启/关闭两种模式核验字号与输入框聚焦不放大。
- **背景**：iPadOS Safari 默认桌面级浏览会把指针伪装成 `fine`/`hover`（Apple WWDC19），故可读性规则的判据已从 `@media (pointer: coarse)` 改为 `@media (pointer: coarse), (max-width: 1376px)`（1376 = iPad Pro 13″ 横屏视口宽，Apple HIG）。已用 1088 px 视口 + fine 指针实测命中（正文 15 px、输入框 16 px），但真机 Safari 未跑。
- **要求**：真机 Safari 打开局域网地址，检查：正文不小于 15 px、输入框聚焦不触发整页放大、无横向溢出；横竖屏各一遍。
- **关联**：[main.css](file:///d:/Workplace/ChageeCupDrawing/frontend/src/assets/main.css) 触摸端两组 `@media`

### 1A.5 局域网模式的访问控制偏弱 ⚠中

- **描述**：`CUPDRAW_BIND=0.0.0.0` 时同网段任意设备可直接控制绘制，缺少设计文档提过的「令牌引导」。
- **背景**：设计文档 A2 曾写「`CUPDRAW_BIND` 局域网模式 + 令牌引导」，实现时沿用了既有会话机制（首次 GET 下发 `cupdraw_session` Cookie + 写请求需 `X-Requested-With: cupdraw`）。该双要件挡的是跨站表单提交，**不挡同网段直接访问**——手机浏览器拿到 Cookie 后即可停止/续画他人的任务。当前为局域网单用户工具，风险可接受，但与设计描述有出入。
- **要求**：确认是接受现状（更新设计文档措辞），还是补一个启动时打印的一次性配对码/访问密码。
- **关联**：[serve.py](file:///d:/Workplace/ChageeCupDrawing/backend/serve.py)、[main.py](file:///d:/Workplace/ChageeCupDrawing/backend/app/main.py) 的 `session_gate`

---

## 2. 环境与清理类

### 2.1 开发机 HTTPS 反代 ✅已停止（2026-10-24）

- **决策**：停止 Caddy 并删除 `deploy/`，开发改回 `http://127.0.0.1:8765` 直连（已实测 `/api/health` = 200）。
- **余留事项**：正式服务器部署时再按 §4.1 重新引入 Caddy（正式域名 + 公共 CA 证书）。

### 2.2 `.trae-html-share-packages/` ✅已删除（2026-10-24）

- **处置**：确认为 Trae 工具产物（仅含旧批次报告的分享副本），已删除；`git status` 不再出现。

---

## 3. 版本管理

### 3.1 全部改动尚未提交 ✅已提交（2026-10-24）

- **处置**：经用户确认全部提交（2026-10-24，单提交 80 文件，+8206/−196），提交信息按子系统分点。工作区已干净（`data/` 运行时数据与 `mc_acceptance` 批次结果被 `.gitignore` 排除）。
- **备注**：未推送远端（用户未要求）；`.benchmarks/mc_acceptance.py` 已随库提交。

---

## 4. 部署与上线类（明确推迟到真实服务器阶段）

### 4.1 服务器部署清单（未开始）

- **描述**：真实服务器上的一次性配置，开发机不做。
- **要求**：
  1. Caddyfile 站点地址改为正式域名，删除 `tls internal`（自动申请 Let's Encrypt，需域名 80/443 可达），按需加 HSTS `preload`；
  2. 进程守护（Linux 用 systemd，Windows 用服务/计划任务）；
  3. 日志轮转、开机自启；
  4. 后端继续只监听 `127.0.0.1:8765`，仅 Caddy 对外。
- **完成判据**：域名 HTTPS 可访问、证书为公共 CA、服务重启后自动恢复。

### 4.2 面向用户的网页版本（方案未定）

- **描述**：用户此前提到「后续可能会开发网页给用户使用」，公网多用户形态尚未设计。
- **背景**：当前是单会话 token（`cupdraw_session` + `X-Requested-With`）、API Key 存浏览器 `localStorage` 并经 `X-AI-Key` 单次携带。单机单用户可用，公网多用户需重新评估会话模型、限流与资源滥用防护。
- **要求**：先出方案再动手，不在开发机预置任何多用户配置。

### 4.3 可选：Linux 容器化需先做 headless 改造（未排期）

- **描述**：曾尝试 Docker 化，因后端在导入期硬依赖 Windows API 而放弃（容器内报 `module 'ctypes' has no attribute 'WinDLL'`），Dockerfile 等已删除。
- **背景**：涉及 6 处导入期 `ctypes.WinDLL`：`device/capture.py`、`device/windows.py`、`device/geometry.py`、`executor/native_mouse.py`、`executor/guard.py`，以及 MaaFw 为 Windows 版。
- **要求**：若确需容器化，先做「无设备 headless」降级（延迟导入 + 平台分支），改造成本较高。
- **完成判据**：容器内 `create_app()` 可启动，编辑类接口可用（设备类接口返回明确降级错误）。

---

## 5. 设计文档中列为可选、尚未排期的里程碑

| 项 | 说明 | 状态 |
| --- | --- | --- |
| B3 ADB 直连 + MaaTouch 注入 | `TargetProvider` 已预留实现位；逐点时延实测后决定是否推进（见 1A.2） | 未开始，等 1A.2 数据 |
| M-D 豆包方舟 provider | 火山引擎图像编辑通道，只收公网 URL 的接口需先有对象存储 | 未开始（`dashscope` 已接入） |
| M-E 语义解析 VLM 升级 | 规则未命中时先问文本模型抽 JSON，再落生成通道 | 未开始，可并行 |
| 本地 SD 自部署通道 | OpenAI 兼容 provider 已支持本地地址（免 Key），未做真实联调 | 通道就绪，未验证 |

---

## 6. 已交付（无需再处理，避免重复劳动）

- M-A 设置与路由、M-B Gemini / OpenAI 兼容 / DashScope 三个 provider + ROI 管线（裁—letterbox—回填—羽化）
- BYOK 安全改造：Key 仅存浏览器 `localStorage`，经 `X-AI-Key` 单次携带；服务端 `ContextVar` 注入、启动清盘、不回显
- 设置页修复：提供商联动回填、模型候选 chip、预检与保存状态分离、清除 Key 可逆、预检真实连通性
- HTTPS 反代全链路（200 / 308 / 安全头 / `Secure` Cookie / 浏览器无告警）
- 开机自启已按要求移除（`start-*.cmd` + 启动文件夹快捷方式）
- **G0/G1 输入主权守护**：前台丢失 / 最小化 / 光标接管 / 物理左键 / ESC 五路信号，50 ms 轮询；无手机 E2E 实测 62 ms 抬笔；倒计时改为冻结式（切走不失败）
- **断点续画贯通守护场景**：`STOPPED(stopReason=focus_lost|mouse_takeover|esc)` → resume 从原笔画位置续到 DONE，实测断点 2/6 无损
- **B1 `DrawingTarget` 抽象**：执行/守护/面板/标定全部面向 target，新增目标类型不再改执行路径
- **B2 镜像目标**：`MirrorService`（scrcpy 启动 + adb 枚举 + 注册表持久化）+ `MirrorTarget` + `GET/POST/DELETE /api/devices*` + 校准记录带 `deviceKind/deviceSerial`
- **多设备控制端**：全局 `.split` 断点体系（<1024px 单列）、触摸目标 ≥44 px、iPad 字号判据修正为 `(pointer: coarse), (max-width: 1376px)`（防 iPadOS 桌面级浏览伪装指针）、`CUPDRAW_BIND` 局域网模式
- 无手机全链路夹具 `backend/tests/manual/mirror_e2e/`（可重复回归镜像链路）
