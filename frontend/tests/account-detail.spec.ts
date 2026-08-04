import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountDetailPage from "@/pages/AccountDetailPage.vue";

vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { provider: "codebuddy", accountId: "cb-main" } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const account = {
  provider: "codebuddy", account_id: "cb-main", label: "主账号", source: "oauth",
  enabled: true, summary_status: "pending", purposes: {
    chat: { enabled: true, status: "active", verification_status: "not_required" },
    checkin: { enabled: false, status: "unconfigured", verification_status: "unverified" },
  },
};

function response(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

describe("AccountDetailPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("confirms a side-effecting inherited check-in before sending the request", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET";
      calls.push({ url, method });
      if (url.endsWith("/verify-checkin")) return response({ status: "ok", run_id: "run-1", results: [] });
      if (url.includes("/credentials")) return response({ credentials: [] });
      if (url.includes("/metrics")) return response({ snapshots: [] });
      if (url.includes("/usage/events")) return response({ events: [] });
      if (url.includes("/checkin/runs")) return response({ runs: [] });
      return response(account);
    }));
    const wrapper = mount(AccountDetailPage, {
      attachTo: document.body,
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });
    await flushPromises();
    expect(wrapper.find(".account-summary-grid").exists()).toBe(true);
    expect(wrapper.find(".detail-main-grid").exists()).toBe(true);
    expect(wrapper.find(".trend-section").exists()).toBe(true);
    expect(wrapper.text()).not.toContain("账号、凭据、指标与签到活动均按用途隔离");

    await wrapper.findAll("button").find((button) => button.text().includes("验证签到"))?.trigger("click");
    expect(document.body.textContent).toContain("可能立即领取当天积分");
    expect(calls.some((call) => call.url.endsWith("/verify-checkin"))).toBe(false);
    document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)")?.click();
    await flushPromises();

    expect(calls).toContainEqual({ url: "/api/admin/accounts/codebuddy/cb-main/verify-checkin", method: "POST" });
    wrapper.unmount();
  });

  it("renders structured metrics and paginates detail histories", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/credentials")) return response({ credentials: [] });
      if (url.includes("/metrics/accounts/")) return response({ snapshots: [{ metric_kind: "quota", status: "fresh", observed_at: "2026-08-04T10:00:00Z", value: { total_usage_percentage: 20, user_quota: { remaining: 80, total: 100, unit: "credits" } } }] });
      if (url.includes("/usage/events")) return response({ events: Array.from({ length: 11 }, (_, index) => ({ event_id: `event-${index + 1}`, model_id: `model-${index + 1}`, status: "succeeded", latency_ms: index + 1, started_at: "2026-08-04T10:00:00Z" })) });
      if (url.includes("/checkin/runs")) return response({ runs: [] });
      return response(account);
    }));
    const wrapper = mount(AccountDetailPage, {
      attachTo: document.body,
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("已使用 20%");
    expect(wrapper.text()).toContain("用户配额 · 剩余 80 / 100 credits");
    expect(wrapper.text()).not.toContain("total_usage_percentage");
    expect(wrapper.text()).not.toContain("该指标暂无可读字段");
    expect(wrapper.text()).toContain("无需验证");
    expect(wrapper.text()).toContain("model-1");
    expect(wrapper.text()).not.toContain("model-11");

    await wrapper.findAll("button").find((button) => button.text() === "下一页")?.trigger("click");
    expect(wrapper.text()).toContain("model-11");
    expect(wrapper.text()).not.toMatch(/model-1(?:\s|成功)/);
    wrapper.unmount();
  });
});
