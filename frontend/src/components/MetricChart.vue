<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from "vue";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([CanvasRenderer, GridComponent, LineChart, TooltipComponent]);

const props = defineProps<{ labels: string[]; values: number[]; color?: string }>();
const chartHost = ref<HTMLElement | null>(null);
let chart: echarts.ECharts | null = null;

// 从设计 token 取色，避免图表与 tokens.css 各自维护一套配色
function token(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function render(): void {
  if (!chartHost.value) return;
  const accent = props.color ?? token("--accent", "#e8913a");
  const axis = token("--faint", "#66666f");
  const line = token("--line", "#26262e");
  chart ??= echarts.init(chartHost.value);
  chart.setOption({
    animation: false,
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: "line", lineStyle: { color: accent, opacity: 0.45 } },
      backgroundColor: token("--surface-raised", "#16161b"),
      borderColor: line,
      textStyle: { color: token("--text", "#e4e4e8"), fontSize: 11, fontFamily: token("--mono", "monospace") },
      formatter: (params: unknown) => formatTooltip(params),
    },
    grid: { left: 8, right: 8, top: 12, bottom: 22, containLabel: true },
    xAxis: {
      type: "category",
      data: props.labels,
      axisLine: { lineStyle: { color: line } },
      axisLabel: { color: axis, fontSize: 10, fontFamily: token("--mono", "monospace") },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: line } },
      axisLabel: { color: axis, fontSize: 10, fontFamily: token("--mono", "monospace") },
    },
    series: [{
      type: "line",
      smooth: true,
      showSymbol: false,
      symbol: "circle",
      symbolSize: 6,
      data: props.values,
      lineStyle: { color: accent, width: 1.5 },
      areaStyle: { color: token("--accent-soft", "rgb(232 145 58 / 0.12)") },
      emphasis: { focus: "series", itemStyle: { color: accent, borderColor: token("--surface", "#101014"), borderWidth: 2 } },
    }],
  });
}

function formatTooltip(params: unknown): string {
  const item = Array.isArray(params) ? params[0] : params;
  if (!item || typeof item !== "object") return "";
  const point = item as { axisValue?: unknown; value?: unknown };
  const label = typeof point.axisValue === "string" ? point.axisValue : "";
  const value = typeof point.value === "number" ? point.value.toLocaleString() : "--";
  return `<div>${label}</div><strong>${value}</strong>`;
}

function resize(): void { chart?.resize(); }
onMounted(() => { render(); window.addEventListener("resize", resize); });
onBeforeUnmount(() => { window.removeEventListener("resize", resize); chart?.dispose(); });
watch(() => [props.labels, props.values], render, { deep: true });
</script>

<template><div ref="chartHost" class="metric-chart" role="img" aria-label="数据趋势图"></div></template>

<style scoped>
.metric-chart {
  width: 100%;
  height: 280px;
  min-height: 280px;
}

@media (max-width: 760px) {
  .metric-chart { height: 240px; min-height: 240px; }
}
</style>
