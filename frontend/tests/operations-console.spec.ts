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
      if (url.includes("/service/events")) return response({ events: [{ event_id: "evt-1", event_type: "lifecycle", action: "start", status: "succeeded", created_at: "2026-07-24T00:00:00Z" }], next_cursor: null, total: 1 });
      if (method === "POST" && url.endsWith("/service/stop")) return response({ operation_id: "op-stop", action: "stop", status: "succeeded", created_at: "2026-07-24T00:00:00Z", finished_at: "2026-07-24T00:00:01Z" });
      return response({ service: "proxy-worker", desired_state: "RUNNING", observed_state: "HEALTHY", in_flight: 2, identity: { pid: 4321, process_start_time: 1_753_315_200, owner_instance_id: "control-a" }, runtime_snapshot_version: 4 });
    }));
    const wrapper = mount(ServicePage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await buttonWithText(wrapper, "停止").trigger("click");
    expect(document.body.textContent).toContain("停止 Proxy Worker");
    await confirmDialog();

    expect(calls).toContainEqual(expect.objectContaining({ url: expect.stringContaining("/service/stop"), method: "POST" }));
    expect(wrapper.text()).toContain("op-stop");
    wrapper.unmount();
  });

  it("probes a model and confirms disabling it", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method });
      if (url.includes("/usage/summary")) return response({ summary: { request_count: 12, success_count: 11, error_count: 1, p95_latency_ms: 240 } });
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
    expect(wrapper.text()).toContain("1 / 1 个账号成功");
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
    await confirmDialog();

    const patch = calls.find((item) => item.method === "PATCH");
    expect(JSON.parse(patch?.body ?? "{}")).toMatchObject({ key: "usage.detail_retention_days", value: 30, value_version: 2 });
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
