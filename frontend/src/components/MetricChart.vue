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

function render(): void {
  if (!chartHost.value) return;
  chart ??= echarts.init(chartHost.value);
  chart.setOption({
    animation: false,
    grid: { left: 8, right: 8, top: 12, bottom: 22, containLabel: true },
    xAxis: { type: "category", data: props.labels, axisLabel: { color: "#718078", fontSize: 10 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#26332e" } }, axisLabel: { color: "#718078", fontSize: 10 } },
    series: [{ type: "line", smooth: true, symbol: "none", data: props.values, lineStyle: { color: props.color ?? "#62d7a2", width: 2 }, areaStyle: { color: "rgba(98,215,162,.1)" } }],
  });
}

function resize(): void { chart?.resize(); }
onMounted(() => { render(); window.addEventListener("resize", resize); });
onBeforeUnmount(() => { window.removeEventListener("resize", resize); chart?.dispose(); });
watch(() => [props.labels, props.values], render, { deep: true });
</script>

<template><div ref="chartHost" class="metric-chart" aria-label="数据趋势图"></div></template>
