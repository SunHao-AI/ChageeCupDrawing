<script setup lang="ts">
/** 校准框选示意图：小程序画板窗口的简化布局，按当前模式高亮目标控件。
 *
 * 内联 SVG（非位图）：高亮随模式即时切换，颜色走主题 CSS 变量。
 */
withDefaults(defineProps<{ mode: 'canvas' | 'slider' | 'undo' | 'fold' }>(), {
  mode: 'canvas',
})
</script>

<template>
  <!-- 按真实画板布局绘制：需先点底部「画笔」进入绘画面板；收起态工具行为
       「∨ 画笔 橡皮擦 ✓」（撤销浮在左侧），点 ∨ 展开后工具行变「∧ …」并在
       下方多出「大小」滑杆。∨ 与 ∧ 是同一位置、功能相反的两个独立按钮。 -->
  <svg viewBox="0 0 340 262" class="diagram" role="img" aria-label="校准位置示意图">
    <!-- 窗口与标题栏 -->
    <rect x="10" y="10" width="320" height="244" rx="10" class="win" />
    <line x1="10" y1="38" x2="330" y2="38" class="barline" />
    <text x="24" y="30" class="label">小程序画板窗口（绘画面板）</text>

    <!-- 画布区 -->
    <g :class="mode === 'canvas' ? 'hot' : 'dim'">
      <rect x="26" y="46" width="288" height="110" rx="4" class="canvas" />
      <path d="M60 128 C 110 70, 170 138, 230 82 S 292 100, 296 90" class="scribble" />
    </g>
    <g v-if="mode === 'canvas'" class="hl">
      <rect x="22" y="42" width="296" height="118" rx="6" />
      <text x="170" y="34" text-anchor="middle" class="anno">按住拖动，框出白色画布区域 ↓</text>
    </g>

    <!-- 撤销：浮在画布左下侧的独立胶囊（展开/收起两态都在） -->
    <g :class="mode === 'undo' ? 'hot' : 'dim'">
      <rect x="26" y="160" width="62" height="20" rx="10" class="pill" />
      <path d="M44 172 a6 6 0 1 1 7 -8" class="glyph-line" />
      <path d="M44 172 l-4 -4 M44 172 l5 -2" class="glyph-line" />
      <text x="66" y="175" class="mini">↪</text>
    </g>
    <g v-if="mode === 'undo'" class="hl">
      <rect x="22" y="156" width="70" height="28" rx="12" />
      <text x="57" y="150" text-anchor="middle" class="anno">点撤销「↩」中心 ↓</text>
    </g>

    <!-- 底部工具条：开合钮 / 画笔 / 橡皮擦 / ✓完成 -->
    <rect x="26" y="188" width="288" height="26" rx="6" class="toolbar" />

    <!-- 开合按钮：同一位置两态两个独立按钮（收起态 ∨＝展开面板，展开态 ∧＝收起面板） -->
    <g :class="mode === 'fold' ? 'hot' : 'dim'">
      <text x="48" y="207" text-anchor="middle" class="glyph">∧∨</text>
    </g>
    <g v-if="mode === 'fold'" class="hl">
      <rect x="32" y="190" width="32" height="22" rx="5" />
      <text x="48" y="184" text-anchor="middle" class="anno">最左开合钮 ↓</text>
    </g>

    <!-- 画笔（进入绘画面板的入口）/ 橡皮擦 / ✓完成：参考位置，随开合钮同行 -->
    <g class="dim">
      <line x1="98" y1="207" x2="110" y2="193" class="glyph-line" />
      <text x="116" y="207" class="mini">画笔</text>
      <rect x="168" y="195" width="16" height="11" rx="2" class="glyph-line" />
      <text x="190" y="207" class="mini">橡皮擦</text>
      <text x="300" y="207" text-anchor="middle" class="glyph">✓</text>
    </g>

    <!-- 笔粗细滑杆：仅展开态（工具行下方多出「大小」一行） -->
    <g :class="mode === 'slider' ? 'hot' : 'dim'">
      <rect x="26" y="222" width="288" height="24" rx="6" class="subbar" />
      <text x="38" y="238" class="mini">大小</text>
      <line x1="70" y1="234" x2="298" y2="234" class="track" />
      <circle cx="150" cy="234" r="6" class="knob" />
    </g>
    <g v-if="mode === 'slider'" class="hl">
      <rect x="64" y="224" width="240" height="20" rx="7" />
      <text x="184" y="256" text-anchor="middle" class="anno">框住整条轨道（贴两端，先点 ∧ 展开）↑</text>
    </g>
  </svg>
</template>

<style scoped>
.diagram {
  display: block;
  width: 100%;
  height: auto;
}

.win {
  fill: var(--ink-2);
  stroke: var(--line);
}

.barline {
  stroke: var(--line-soft);
}

.label {
  fill: var(--text-faint);
  font-size: 11px;
}

.canvas {
  fill: #f5f2ea;
  stroke: var(--line);
}

.scribble {
  fill: none;
  stroke: var(--text-faint);
  stroke-width: 2.5;
  stroke-linecap: round;
  opacity: 0.5;
}

.toolbar {
  fill: var(--ink-3);
  stroke: var(--line-soft);
}

.subbar {
  fill: var(--ink-2);
  stroke: var(--line-soft);
}

.pill {
  fill: var(--ink-3);
  stroke: var(--line-soft);
}

.mini {
  fill: var(--text-dim);
  font-size: 10px;
}

.glyph {
  fill: var(--text-dim);
  font-size: 15px;
  font-weight: 700;
}

.glyph-line {
  stroke: var(--text-dim);
  stroke-width: 2;
  fill: none;
  stroke-linecap: round;
}

.track {
  stroke: var(--text-dim);
  stroke-width: 3;
  stroke-linecap: round;
}

.knob {
  fill: var(--gold);
  stroke: none;
}

.hot {
  opacity: 1;
}

.dim {
  opacity: 0.38;
}

.hl rect {
  fill: none;
  stroke: var(--gold);
  stroke-width: 2;
  stroke-dasharray: 5 3;
}

.anno {
  fill: var(--gold);
  font-size: 11.5px;
  font-weight: 600;
}
</style>
