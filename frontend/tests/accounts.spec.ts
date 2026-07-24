import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountsPage from "@/pages/AccountsPage.vue";
import AccountImportPanel from "@/components/AccountImportPanel.vue";

const account = {
  provider: "qoder",
  account_id: "qd-demo",
  label: "研发账号",
  source: "manual",
  enabled: true,
  summary_status: "active",
  purposes: {
    chat: { enabled: true, status: "active", verification_status: "not_required" },
    checkin: { enabled: true, status: "active", verification_status: "verified" },
  },
};

describe("AccountsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders quota and unknown-points snapshots without exposing raw data", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string) => ({
      ok: true,
      json: async () => input.includes("metrics") ? {
        snapshots: [
          { provider: "qoder", account_id: "qd-demo", metric_kind: "quota", status: "fresh", observed_at: "2026-07-23T00:00:00+00:00", value: { total_usage_percentage: 37 } },
          { provider: "qoder", account_id: "qd-demo", metric_kind: "points", status: "unknown", observed_at: "2026-07-23T00:00:00+00:00", value: null, last_error: "protocol_not_verified" },
        ],
      } : { accounts: [account] },
    })));
    const wrapper = mount(AccountsPage, {
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });

    await flushPromises();
    await wrapper.find(".table-link").trigger("click");
    await flushPromises();

    expect(document.body.textContent).toContain("已使用 37%");
    expect(document.body.textContent).toContain("接口协议尚未验证");
    expect(document.body.textContent).not.toContain("protocol_not_verified");
  });

  it("keeps env accounts read-only and reports per-account batch failures", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    let removeFailing = false;
    const envAccount = { ...account, account_id: "qd-env", label: "环境账号", source: "env" };
    const failingAccount = { ...account, account_id: "qd-fail", label: "失败账号" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method });
      if (url.includes("/metrics/accounts")) return response({ snapshots: [] });
      if (method === "POST" && url.includes("qd-fail")) return response({ detail: "probe_failed" }, 422);
      if (method === "POST") return response({ status: "succeeded" });
      return response({ accounts: removeFailing ? [envAccount, account] : [envAccount, account, failingAccount], next_cursor: null });
    }));
    const wrapper = mount(AccountsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    expect(wrapper.get('input[aria-label="环境变量账号不可选择 环境账号"]').attributes("disabled")).toBeDefined();
    expect(wrapper.get('button[aria-label="环境变量账号不可停用 环境账号"]').attributes("disabled")).toBeDefined();
    await wrapper.get('button[aria-label="刷新 环境账号"]').trigger("click");
    await flushPromises();
    expect(calls).toContainEqual({ url: "/api/admin/accounts/qoder/qd-env/refresh", method: "POST" });

    await wrapper.get('input[aria-label="选择 研发账号"]').setValue(true);
    await wrapper.get('input[aria-label="选择 失败账号"]').setValue(true);
    const batchProbe = wrapper.findAll("button").find((button) => button.text().includes("批量探测"));
    await batchProbe?.trigger("click");
    document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)")?.click();
    await flushPromises();

    expect(wrapper.text()).toContain("研发账号");
    expect(wrapper.text()).toContain("失败账号");
    expect(wrapper.text()).toContain("probe_failed");
    expect(calls.some((item) => item.method === "PATCH" && item.url.includes("qd-env"))).toBe(false);

    await wrapper.get('input[aria-label="选择 研发账号"]').setValue(true);
    await wrapper.get('input[aria-label="选择 失败账号"]').setValue(true);
    removeFailing = true;
    await wrapper.get('button[aria-label="刷新 环境账号"]').trigger("click");
    await flushPromises();
    const batchDisable = wrapper.findAll("button").find((button) => button.text().includes("批量停用"));
    await batchDisable?.trigger("click");
    document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)")?.click();
    await flushPromises();

    expect(wrapper.text()).toContain("account_not_on_current_page");
    expect(wrapper.text()).toContain("1 跳过");
    expect(calls.some((item) => item.method === "PATCH" && item.url.includes("qd-env"))).toBe(false);
  });

  it("serializes all single and batch mutations while one account request is pending", async () => {
    let releaseMutation!: () => void;
    const blocked = new Promise<void>((resolve) => { releaseMutation = resolve; });
    const mutations: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET";
      if (url.includes("/metrics/accounts")) return response({ snapshots: [] });
      if (method !== "GET") { mutations.push(url); await blocked; return response({ status: "succeeded" }); }
      return response({ accounts: [account], next_cursor: null });
    }));
    const wrapper = mount(AccountsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await wrapper.get('input[aria-label="选择 研发账号"]').setValue(true);
    await wrapper.get('button[aria-label="刷新 研发账号"]').trigger("click");
    await flushPromises();

    for (const label of ["刷新 研发账号", "探测 研发账号", "停用 研发账号", "删除 研发账号"]) {
      expect(wrapper.get(`button[aria-label="${label}"]`).attributes("disabled")).toBeDefined();
    }
    for (const text of ["批量刷新", "批量探测", "批量停用"]) {
      expect(wrapper.findAll("button").find((button) => button.text().includes(text))?.attributes("disabled")).toBeDefined();
    }

    await wrapper.get('button[aria-label="探测 研发账号"]').trigger("click");
    await wrapper.findAll("button").find((button) => button.text().includes("批量探测"))?.trigger("click");
    expect(document.querySelector(".confirm-dialog")).toBeNull();
    expect(mutations).toEqual(["/api/admin/accounts/qoder/qd-demo/refresh"]);

    releaseMutation();
    await flushPromises();
    expect(mutations).toHaveLength(1);
    wrapper.unmount();
  });
});

describe("AccountImportPanel", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it("sends a verified WorkBuddy cookie import without rendering the cookie", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return response({ status: "ok", account: { provider: "codebuddy", account_id: "cb-main", label: "主账号" } });
    }));
    const wrapper = mount(AccountImportPanel, { global: { plugins: [createPinia()] } });
    const checkin = wrapper.findAll("button").find((button) => button.text() === "Check-in");
    await checkin?.trigger("click");
    await wrapper.get('input[aria-label="账号 ID"]').setValue("cb-main");
    await wrapper.get('select[aria-label="Check-in 认证模式"]').setValue("cookie");
    await wrapper.get('input[aria-label="WorkBuddy Cookie"]').setValue("session=secret");
    const submit = wrapper.findAll("button").find((button) => button.text().includes("验证并启用"));
    await submit?.trigger("click");
    document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)")?.click();
    await flushPromises();

    expect(calls[0].url).toBe("/api/admin/auth/codebuddy/checkin");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({ account_id: "cb-main", mode: "cookie", cookie: "session=secret" });
    expect(document.querySelector(".confirm-dialog")).toBeNull();
    expect(wrapper.text()).not.toContain("session=secret");
    expect(wrapper.text()).toContain("凭据已验证并保存");
  });

});

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}
