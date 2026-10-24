/**
 * 生成通道服务目录：国内外常用图像编辑模型、价格与 Key 申请步骤。
 *
 * status 表示与本项目当前接入能力的关系：
 * - ready：已接入，选中即自动填好 provider / 模型 / 服务地址；
 * - native：厂商为原生「提交+轮询」协议，暂未接入，仅提供申请指引；
 * - text：纯文本模型，没有图像编辑端点，选中也不能改图。
 *
 * 价格为 2026-10 调研近似值，接入或充值前请以官网现价为准。
 */

export type CatalogRegion = '海外' | '国内' | '本地'
export type CatalogStatus = 'ready' | 'native' | 'text'
export type CatalogPrice = '免费' | '收费' | '免费额度'

export interface AiCatalogEntry {
  id: string
  name: string
  vendor: string
  region: CatalogRegion
  status: CatalogStatus
  price: CatalogPrice
  priceNote: string
  /** ready 条目才会在选中时回填这三项。 */
  provider?: string
  model?: string
  baseUrl?: string
  /** 该服务可选模型，供「模型」输入框的下拉候选。 */
  models?: string[]
  keyUrl?: string
  keyLabel?: string
  steps: string[]
  note?: string
}

export const AI_CATALOG: AiCatalogEntry[] = [
  {
    id: 'gemini-3.1-flash-image',
    name: 'Gemini 3.1 Flash Image（Nano Banana 2）',
    vendor: 'Google',
    region: '海外',
    status: 'ready',
    price: '收费',
    priceNote: '约 $0.067/张（1K 输出，按张计费）',
    provider: 'gemini-image',
    model: 'gemini-3.1-flash-image',
    models: ['gemini-3.1-flash-image', 'gemini-3.1-flash-lite-image', 'gemini-3-pro-image'],
    keyUrl: 'https://aistudio.google.com/apikey',
    keyLabel: 'Google AI Studio → Create API key',
    steps: [
      '用 Google 账号登录 Google AI Studio（需能访问 Google 服务的网络）。',
      '点击 Create API key，选择或新建一个 Google Cloud 项目。',
      '复制 AIza 开头的 Key，粘贴到下方「API Key」。',
      '勾选「允许上传到第三方服务」，保存后点「配置预检」。',
    ],
    note: '指令式编辑、中文艺术字表现最好，是本项目的推荐默认。旧模型 gemini-2.5-flash-image 已于 2026-10-02 关停，请勿再使用。',
  },
  {
    id: 'gemini-3-pro-image',
    name: 'Gemini 3 Pro Image（Nano Banana Pro）',
    vendor: 'Google',
    region: '海外',
    status: 'ready',
    price: '收费',
    priceNote: '约 $0.13/张（约为 Flash 的两倍）',
    provider: 'gemini-image',
    model: 'gemini-3-pro-image',
    models: ['gemini-3-pro-image', 'gemini-3.1-flash-image', 'gemini-3.1-flash-lite-image'],
    keyUrl: 'https://aistudio.google.com/apikey',
    keyLabel: 'Google AI Studio → Create API key',
    steps: [
      '与 Flash 版共用同一个 Google AI Studio Key，无需重复申请。',
      '若已配置过 Key，只需把下方模型改成 gemini-3-pro-image 并保存。',
      '预算敏感时先用 Flash 验证指令，再切到 Pro 出终稿。',
    ],
    note: '构图和精细文字更强，单价约翻倍；适合复杂版式或高精度艺术字。',
  },
  {
    id: 'gpt-image-1.5',
    name: 'GPT Image 1.5',
    vendor: 'OpenAI',
    region: '海外',
    status: 'ready',
    price: '收费',
    priceNote: '按质量档计费，约 $0.01–0.17/张（质量越高越贵）',
    provider: 'openai-compatible',
    model: 'gpt-image-1.5',
    models: [
      'gpt-image-1.5',
      'gpt-image-2',
      'gpt-image-2.5-sunburst',
      'gpt-image-2.5-flare',
      'gpt-image-1-mini',
      'gpt-image-1',
    ],
    baseUrl: 'https://api.openai.com/v1',
    keyUrl: 'https://platform.openai.com/api-keys',
    keyLabel: 'OpenAI Platform → API keys',
    steps: [
      '注册并登录 OpenAI Platform，完成手机验证。',
      '在 Billing 页充值（按量计费，余额不足会直接拒绝请求）。',
      '进入 API keys → Create new secret key，复制 Key（只显示一次）。',
      '粘贴到下方并勾选云上传授权，保存后点「配置预检」。',
    ],
    note: '走 /images/edits 接口，原生支持遮罩局部重绘；需网络可达 OpenAI。',
  },
  {
    id: 'local-openai',
    name: '本地部署（SD / FLUX / vLLM 网关）',
    vendor: '本机',
    region: '本地',
    status: 'ready',
    price: '免费',
    priceNote: '免费，仅消耗电费与显卡时间',
    provider: 'openai-compatible',
    model: 'sdxl-inpaint',
    models: ['sdxl-inpaint', 'flux-dev', 'flux-schnell', 'sd15-inpaint'],
    baseUrl: 'http://127.0.0.1:8000/v1',
    steps: [
      '在本机启动带 OpenAI 兼容 /images/edits 的服务（如 vLLM、ComfyUI 网关）。',
      '确认服务地址形如 http://127.0.0.1:端口/v1，端口按实际修改。',
      '把下方模型改成服务里实际加载的 inpaint 模型名（示例 sdxl-inpaint）。',
      '无需 API Key，也无需勾选云上传授权；点「配置预检」确认可达。',
    ],
    note: '隐私最好、零费用，速度取决于显卡。纯文本模型（如 qwen3.8-flash）没有图像端点，会返回 404「不支持图片编辑接口」。',
  },
  {
    id: 'dashscope-imageedit',
    name: '通义 图像编辑（qwen-image-2.0）',
    vendor: '阿里云百炼',
    region: '国内',
    status: 'ready',
    price: '免费额度',
    priceNote: '按张计费，以百炼控制台现价为准；新用户有免费额度',
    provider: 'dashscope',
    model: 'qwen-image-2.0',
    models: ['qwen-image-2.0', 'qwen-image-2.0-pro', 'qwen-image-edit', 'qwen-image-edit-plus'],
    baseUrl: 'https://dashscope.aliyuncs.com',
    keyUrl: 'https://bailian.console.aliyun.com/',
    keyLabel: '阿里云百炼控制台 → API-KEY 管理',
    steps: [
      '用阿里云账号登录百炼控制台，完成实名认证。',
      '开通「百炼大模型服务」，在 API-KEY 管理页创建并复制 Key。',
      '在模型广场开通千问图像编辑（qwen-image-2.0 系列）。',
      '粘贴 Key、勾选云上传授权，保存后点「配置预检」。',
    ],
    note: '走百炼多模态编辑接口，本地选区块以 data URL 直传，无需先传公网图床。早期模型 qwen-image-edit 已停止服务（报 Model not exist），请选用 qwen-image-2.0 系列。',
  },
  {
    id: 'doubao-seededit',
    name: '豆包 SeedEdit 3.0（doubao-seededit-3.0-i2i）',
    vendor: '火山引擎方舟',
    region: '国内',
    status: 'native',
    price: '免费额度',
    priceNote: '0.30 元/张；新用户 500 次体验额度',
    models: ['doubao-seededit-3.0-i2i'],
    provider: 'volcengine-ark',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    keyUrl: 'https://console.volcengine.com/ark',
    keyLabel: '火山方舟控制台 → API Key 管理',
    steps: [
      '登录火山引擎控制台并完成实名认证。',
      '进入方舟 → 开通管理，开通 SeedEdit 图像编辑模型。',
      '在 API Key 管理页创建并复制 Key。',
      '当前版本尚未适配方舟原生接口，可先保存 Key，等 M-D 接入。',
    ],
    note: '方舟原生接口，非 OpenAI 兼容；中文文案理解好，适合国内直连场景。',
  },
  {
    id: 'deepseek-text',
    name: 'DeepSeek（deepseek-chat）',
    vendor: '深度求索',
    region: '国内',
    status: 'text',
    price: '收费',
    priceNote: '按 token 计费，单价低',
    models: ['deepseek-chat', 'deepseek-reasoner'],
    provider: 'deepseek',
    baseUrl: 'https://api.deepseek.com/v1',
    keyUrl: 'https://platform.deepseek.com/api_keys',
    keyLabel: 'DeepSeek 开放平台 → API keys',
    steps: [
      '注册并登录 DeepSeek 开放平台。',
      '充值后进入 API keys → 创建 Key。',
      '它只有文本对话接口，不能改图，请勿填成生成 provider。',
    ],
    note: '仅文本。若把纯文本网关当生成通道，生成时会报「不支持图片编辑接口 / 404」。',
  },
  {
    id: 'qwen-text',
    name: '通义千问 文本模型（qwen3.8-flash 等）',
    vendor: '阿里云百炼',
    region: '国内',
    status: 'text',
    price: '收费',
    priceNote: '按 token 计费（有免费额度）',
    models: ['qwen3.8-flash', 'qwen3.8-plus'],
    provider: 'dashscope',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    keyUrl: 'https://bailian.console.aliyun.com/',
    keyLabel: '阿里云百炼控制台 → API-KEY 管理',
    steps: [
      '登录百炼控制台并创建 API Key。',
      'qwen 系列是文本/视觉理解模型，没有图像生成端点。',
      '要改图请选上面的「通义 图像编辑」或 Gemini / GPT Image。',
    ],
    note: '仅文本。vLLM 网关常把文本模型和图像模型混部，请先确认 /images/edits 可用。',
  },
]

/** 按能力分组，供下拉框的 optgroup 使用。 */
export const CATALOG_GROUPS: { status: CatalogStatus; label: string; entries: AiCatalogEntry[] }[] = [
  { status: 'ready', label: '可直接使用', entries: AI_CATALOG.filter((e) => e.status === 'ready') },
  { status: 'native', label: '暂未接入（可先申请 Key）', entries: AI_CATALOG.filter((e) => e.status === 'native') },
  { status: 'text', label: '仅文本，不支持改图', entries: AI_CATALOG.filter((e) => e.status === 'text') },
]

export const STATUS_TEXT: Record<CatalogStatus, string> = {
  ready: '已接入',
  native: '暂未接入',
  text: '仅文本',
}

const PROVIDER_MODELS: Record<string, string[]> = {
  'gemini-image': ['gemini-3.1-flash-image', 'gemini-3.1-flash-lite-image', 'gemini-3-pro-image'],
  'openai-compatible': [
    'gpt-image-1.5',
    'gpt-image-2',
    'gpt-image-2.5-sunburst',
    'gpt-image-2.5-flare',
    'gpt-image-1-mini',
    'gpt-image-1',
  ],
  'dashscope': ['qwen-image-2.0', 'qwen-image-2.0-pro', 'qwen-image-edit', 'qwen-image-edit-plus'],
}

/** 「模型」输入框的下拉候选：优先用所选服务的模型，未选服务时按 provider 兜底。 */
export function modelOptions(provider: string, entry: AiCatalogEntry | null): string[] {
  if (entry?.models?.length) return entry.models
  return PROVIDER_MODELS[provider] ?? []
}

/** 从已保存设置反查目录条目，用于页面回显。 */
export function matchCatalog(provider: string, model: string, baseUrl: string): AiCatalogEntry | null {
  const exact = AI_CATALOG.find(
    (e) => e.status === 'ready' && e.provider === provider && e.model === model && (e.baseUrl ?? '') === baseUrl,
  )
  if (exact) return exact
  const byModel = AI_CATALOG.find((e) => e.status === 'ready' && e.model === model)
  if (byModel) return byModel
  const byBase = AI_CATALOG.find((e) => e.status === 'ready' && e.baseUrl && e.baseUrl === baseUrl)
  if (byBase) return byBase
  return AI_CATALOG.find((e) => e.status === 'ready' && e.provider === provider) ?? null
}
