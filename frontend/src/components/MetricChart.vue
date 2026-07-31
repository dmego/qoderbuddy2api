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
      smooth: false,
      symbol: "none",
      data: props.values,
      lineStyle: { color: accent, width: 1.5 },
      areaStyle: { color: token("--accent-soft", "rgb(232 145 58 / 0.12)") },
    }],
  });
}

function resize(): void { chart?.resize(); }
onMounted(() => { render(); window.addEventListener("resize", resize); });
onBeforeUnmount(() => { window.removeEventListener("resize", resize); chart?.dispose(); });
watch(() => [props.labels, props.values], render, { deep: true });
</script>

<template><div ref="chartHost" class="metric-chart" aria-label="数据趋势图"></div></template>
