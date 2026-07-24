import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountsPage from "@/pages/AccountsPage.vue";
import AuditPage from "@/pages/AuditPage.vue";
import ModelsPage from "@/pages/ModelsPage.vue";
import ServicePage from "@/pages/ServicePage.vue";
import SettingsPage from "@/pages/SettingsPage.vue";

describe("operations console workflows", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("confirms a service stop and records the completed operation", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method });
      if (url.includes("/service/events")) return response({ events: [{ event_id: "evt-1", event_type: "state", action: "start", status: "succeeded", in_flight: 2, created_at: "2026-07-24T00:00:00Z" }], next_cursor: null });
      if (method === "POST" && url.endsWith("/service/stop")) return response({ operation_id: "op-stop", action: "stop", status: "succeeded", created_at: "2026-07-24T00:00:00Z", finished_at: "2026-07-24T00:00:01Z" });
      return response({ service: "proxy-worker", desired_state: "RUNNING", observed_state: "HEALTHY", in_flight: 2, identity: { pid: 4321, process_start_time: 1_753_315_200, owner_instance_id: "control-a" }, runtime_snapshot_version: 4 });
    }));
    const wrapper = mount(ServicePage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await buttonWithText(wrapper, "停止").trigger("click");
    expect(document.body.textContent).toContain("停止代理服务");
    await confirmDialog();

    expect(calls).toContainEqual(expect.objectContaining({ url: expect.stringContaining("/service/stop"), method: "POST" }));
    expect(wrapper.text()).toContain("op-stop");
    wrapper.unmount();
  });

  it("probes a model and confirms disabling it", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method });
      if (url.includes("/usage/summary")) return response({ summary: { request_count: 12, success_count: 11, error_count: 1, latency_avg_ms: 120, latency_p95_ms: 240 } });
      if (method === "POST" && url.endsWith("/probe")) return response({ status: "succeeded", latency_ms: 132, checked_at: "2026-07-24T00:00:00Z" });
      if (method === "PATCH") return response({ provider: "qoder", model_id: "model-a", enabled: false });
      return response({ models: [{ provider: "qoder", model_id: "model-a", display_name: "Model A", capabilities: ["chat", "streaming"], source: "definition", enabled: true, last_seen_at: "2026-07-24T00:00:00Z" }], total: 1, next_cursor: null });
    }));
    const wrapper = mount(ModelsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await buttonWithText(wrapper, "探测").trigger("click"); await flushPromises();
    expect(wrapper.text()).toContain("132 ms");
    await wrapper.get('button[aria-label="停用 model-a"]').trigger("click");
    await confirmDialog();

    expect(calls.some((item) => item.method === "POST" && item.url.endsWith("/probe"))).toBe(true);
    expect(calls.some((item) => item.method === "PATCH" && item.url.includes("/models/qoder/model-a"))).toBe(true);
    wrapper.unmount();
  });

  it("runs a batch account probe with per-account management contracts", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method });
      if (url.includes("/metrics/accounts")) return response({ snapshots: [] });
      if (method === "POST" && url.endsWith("/probe")) return response({ status: "succeeded", latency_ms: 88 });
      return response({ accounts: [{ provider: "qoder", account_id: "qd-1", label: "研发账号", source: "manual", enabled: true, summary_status: "active", purposes: { chat: { enabled: true, status: "active", verification_status: "verified" } } }], total: 1, next_cursor: null });
    }));
    const wrapper = mount(AccountsPage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { AccountImportPanel: true } } });
    await flushPromises();

    await wrapper.get('input[aria-label="选择 研发账号"]').setValue(true);
    await buttonWithText(wrapper, "批量探测").trigger("click");
    await confirmDialog();

    expect(calls.some((item) => item.method === "POST" && item.url.endsWith("/accounts/qoder/qd-1/probe"))).toBe(true);
    expect(wrapper.text()).toContain("1 成功 · 0 失败 · 0 跳过");
    wrapper.unmount();
  });

  it("requires typed confirmation before reducing usage retention", async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method, body: init?.body as string | undefined });
      if (method === "PATCH") return response({ key: "usage.detail_retention_days", value: 30, value_version: 3, apply_mode: "immediate", apply_status: "effective" });
      return response({ settings: [{ key: "usage.detail_retention_days", value: 90, value_version: 2, source: "database", apply_mode: "immediate", apply_status: "effective" }], schema: { "usage.detail_retention_days": { type: "int", apply_mode: "immediate" } } });
    }));
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await wrapper.get('input[aria-label="请求明细保留天数"]').setValue("30");
    await wrapper.get('button[aria-label="保存 请求明细保留天数"]').trigger("click");
    const confirm = document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)");
    expect(confirm?.disabled).toBe(true);
    const verification = document.querySelector<HTMLInputElement>(".dialog-verification input");
    verification!.value = "RETENTION"; verification!.dispatchEvent(new Event("input", { bubbles: true })); await flushPromises();
    await wrapper.get('input[aria-label="请求明细保留天数"]').setValue("15");
    await confirmDialog();

    const patch = calls.find((item) => item.method === "PATCH");
    expect(JSON.parse(patch?.body ?? "{}")).toMatchObject({ key: "usage.detail_retention_days", value: 30, value_version: 2 });
    wrapper.unmount();
  });

  it("uses immutable setting snapshots across delayed sequential patches", async () => {
    const bodies: Array<{ key: string; value: unknown; value_version: number }> = [];
    let releaseFirst!: () => void;
    const firstPatch = new Promise<void>((resolve) => { releaseFirst = resolve; });
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET") return response(settingsPayload);
      const body = JSON.parse(String(init?.body)) as { key: string; value: unknown; value_version: number };
      bodies.push(body);
      if (bodies.length === 1) await firstPatch;
      return response({ ...body, value_version: body.value_version + 1, apply_mode: "immediate", apply_status: "effective" });
    }));
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await wrapper.get('input[aria-label="代理进程启动超时"]').setValue("45");
    await wrapper.get('input[aria-label="账号指标刷新间隔"]').setValue("120");
    await buttonWithText(wrapper, "保存全部").trigger("click");
    await flushPromises();

    const secondInput = wrapper.get<HTMLInputElement>('input[aria-label="账号指标刷新间隔"]');
    expect(secondInput.attributes("disabled")).toBeDefined();
    secondInput.element.value = "240";
    secondInput.element.dispatchEvent(new Event("input", { bubbles: true }));
    releaseFirst();
    await flushPromises();

    expect(bodies.map((item) => [item.key, item.value])).toEqual([
      ["service.worker.start_timeout_seconds", 45],
      ["monitoring.metrics_interval_seconds", 120],
    ]);
    wrapper.unmount();
  });

  it.each([
    [409, "version_conflict"],
    [422, "setting_apply_failed"],
  ])("persists two setting successes and one %i failure before refetching", async (failureStatus, failureCode) => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    let getCount = 0;
    const serverValues: Record<string, unknown> = {
      "service.worker.start_timeout_seconds": 30,
      "monitoring.metrics_interval_seconds": 60,
      "checkin.at": "08:00",
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method, body: init?.body as string | undefined });
      if (method === "GET") { getCount += 1; return response({ ...settingsPayload, settings: settingsPayload.settings.map((item) => ({ ...item, value: serverValues[item.key] })) }); }
      const body = JSON.parse(String(init?.body)) as { key: string; value: unknown; value_version: number };
      if (body.key === "checkin.at") return response({ detail: failureCode }, failureStatus);
      serverValues[body.key] = body.value;
      return response({ ...body, value_version: body.value_version + 1, apply_mode: "immediate", apply_status: "effective" });
    }));
    const wrapper = mount(SettingsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await wrapper.get('input[aria-label="代理进程启动超时"]').setValue("45");
    await wrapper.get('input[aria-label="账号指标刷新间隔"]').setValue("120");
    await wrapper.get('input[aria-label="签到时间"]').setValue("09:00");
    await buttonWithText(wrapper, "保存全部").trigger("click");
    await flushPromises();

    expect(calls.filter((item) => item.method === "PATCH")).toHaveLength(3);
    expect(getCount).toBeGreaterThan(1);
    expect(wrapper.text()).toContain("代理进程启动超时");
    expect(wrapper.text()).toContain("账号指标刷新间隔");
    expect(wrapper.text()).toContain("签到时间");
    expect(wrapper.text()).toContain(failureCode);
    expect(wrapper.get<HTMLInputElement>('input[aria-label="代理进程启动超时"]').element.value).toBe("45");
    expect(wrapper.get<HTMLInputElement>('input[aria-label="账号指标刷新间隔"]').element.value).toBe("120");
    expect(wrapper.get<HTMLInputElement>('input[aria-label="签到时间"]').element.value).toBe("09:00");
    expect(wrapper.get('button[aria-label="保存 签到时间"]').attributes("disabled")).toBeUndefined();
    wrapper.unmount();
  });

  it("creates a backup and validates restore in dry-run mode", async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method, body: init?.body as string | undefined });
      if (method === "POST" && url.endsWith("/backup")) return response({ backup_id: "backup-new", status: "succeeded", schema_version: "4", size_bytes: 4096 });
      if (method === "POST" && url.endsWith("/restore")) return response({ backup_id: "backup-1", status: "succeeded", checksum: "valid", integrity: "ok" });
      if (url.includes("/audit")) return response({ events: [], next_cursor: null, total: 0 });
      return response({ backups: [{ backup_id: "backup-1", status: "succeeded", schema_version: "4", size_bytes: 2048, sha256: "abc123", started_at: "2026-07-24T00:00:00Z" }] });
    }));
    const wrapper = mount(AuditPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await buttonWithText(wrapper, "创建 SQLite 备份").trigger("click"); await confirmDialog();
    await buttonWithText(wrapper, "校验恢复").trigger("click"); await confirmDialog();

    expect(calls.some((item) => item.method === "POST" && item.url.endsWith("/backup"))).toBe(true);
    const restore = calls.find((item) => item.method === "POST" && item.url.endsWith("/restore"));
    expect(JSON.parse(restore?.body ?? "{}")).toEqual({ dry_run: true });
    expect(wrapper.text()).toContain("校验离线恢复");
    wrapper.unmount();
  });
});

function buttonWithText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(text));
  if (!button) throw new Error(`Button not found: ${text}`);
  return button;
}

async function confirmDialog(): Promise<void> {
  const button = document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)");
  if (!button) throw new Error("Confirmation button not found");
  button.click(); await flushPromises();
}

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body, blob: async () => new Blob([JSON.stringify(body)]) } as Response;
}

const settingsPayload = {
  settings: [
    { key: "service.worker.start_timeout_seconds", value: 30, value_version: 1, source: "database", apply_mode: "immediate", apply_status: "effective" },
    { key: "monitoring.metrics_interval_seconds", value: 60, value_version: 1, source: "database", apply_mode: "immediate", apply_status: "effective" },
    { key: "checkin.at", value: "08:00", value_version: 1, source: "database", apply_mode: "immediate", apply_status: "effective" },
  ],
  schema: {
    "service.worker.start_timeout_seconds": { type: "int", apply_mode: "immediate" },
    "monitoring.metrics_interval_seconds": { type: "int", apply_mode: "immediate" },
    "checkin.at": { type: "str", apply_mode: "immediate" },
  },
};
