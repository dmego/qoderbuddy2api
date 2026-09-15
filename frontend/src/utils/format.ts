// 数字与时间的展示格式化工具。
// 系统内时间统一以 UTC 存储（无后缀或带 +00:00 的 ISO），这里统一转成东八区（北京时间）展示。

const BEIJING_TZ = "Asia/Shanghai";

/** 将 token/数量换算成 K / M 单位（>=1k 显示一位小数，保留整数不带小数）。 */
export function formatTokens(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const abs = Math.abs(value);
  const trim = (s: string) => s.replace(/\.0$/, "");
  if (abs >= 1_000_000) return `${trim((value / 1_000_000).toFixed(1))}M`;
  if (abs >= 1_000) return `${trim((value / 1_000).toFixed(1))}K`;
  return String(value);
}

/** 将存储的 UTC 时间串转成北京时间 "MM/DD HH:mm"。兼容无后缀、Z、带偏移三类输入。 */
export function formatBeijing(value?: string | number): string {
  if (value == null || value === "") return "--";
  const text = String(value).trim();
  if (!text) return "--";
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(text);
  const normalized = hasTz ? text : `${text.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.valueOf())) return text;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: BEIJING_TZ,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

/** 秒以上按秒显示（保留一位小数），不足一秒按毫秒显示，避免大数值堆在毫秒列。 */
const SECONDS_THRESHOLD_MS = 1000;

/** 把毫秒耗时渲染成自适应单位：<1s 用 ms，>=1s 用 s（一位小数）。 */
export function formatDuration(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "--";
  const abs = Math.abs(value);
  if (abs < SECONDS_THRESHOLD_MS) return `${Math.round(value)} ms`;
  const seconds = value / SECONDS_THRESHOLD_MS;
  // 一分钟以上用分:秒，避免 "612.3 s" 这种需要读者自己换算的写法。
  if (Math.abs(seconds) >= 60) {
    const sign = seconds < 0 ? "-" : "";
    const total = Math.abs(seconds);
    const minutes = Math.floor(total / 60);
    const rest = total - minutes * 60;
    return `${sign}${minutes}m ${rest.toFixed(1)}s`;
  }
  return `${seconds.toFixed(1)} s`;
}
