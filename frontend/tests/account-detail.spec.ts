import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountDetailPage from "@/pages/AccountDetailPage.vue";

const routeParams = { provider: "codebuddy", accountId: "cb-main" };
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: routeParams }),
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
    expect(wrapper.find(".points-detail-section").exists()).toBe(true);
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
      if (url.includes("/metrics/accounts/")) return response({ snapshots: [{ metric_kind: "points", status: "fresh", observed_at: "2026-08-04T10:00:00Z", value: { total_remaining: 120, unit: "credits", packages: [{ name: "积分包 1", remaining: 20, total: 20, unit: "credits" }] } }, { metric_kind: "quota", status: "fresh", observed_at: "2026-08-04T10:00:00Z", value: { total_usage_percentage: 20, user_quota: { remaining: 80, total: 100, unit: "credits" } } }, { metric_kind: "activity", status: "fresh", observed_at: "2026-08-04T10:00:00Z", value: { activities: [{ model: "Qwen", remaining: 4, limit: 10 }] } }] });
      if (url.includes("/usage/events")) return response({ events: Array.from({ length: 11 }, (_, index) => ({ event_id: `event-${index + 1}`, model_id: `model-${index + 1}`, status: "succeeded", latency_ms: index + 1, started_at: "2026-08-04T10:00:00Z" })) });
      if (url.includes("/checkin/runs")) return response({ runs: [] });
      return response(account);
    }));
    const wrapper = mount(AccountDetailPage, {
      attachTo: document.body,
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("120 credits");
    expect(wrapper.text()).toContain("积分包 1");
    expect(wrapper.text()).toContain("积分明细");
    expect(wrapper.text()).toContain("用户配额");
    expect(wrapper.text()).toContain("20");
    expect(wrapper.text()).toContain("80");
    expect(wrapper.text()).toContain("总量");
    expect(wrapper.text()).toContain("已用");
    expect(wrapper.text()).toContain("剩余");
    expect(wrapper.text()).not.toContain("total_usage_percentage");
    expect(wrapper.text()).not.toContain("该指标暂无可读字段");
    expect(wrapper.text()).toContain("无需验证");
    expect(wrapper.text()).toContain("model-1");
    expect(wrapper.text()).not.toContain("model-11");

    await wrapper.find(".detail-main-grid .paged-section").findAll("button").find((button) => button.text() === "下一页" && !(button.element as HTMLButtonElement).disabled)?.trigger("click");
    expect(wrapper.text()).toContain("model-11");
    expect(wrapper.text()).not.toMatch(/model-1(?:\s|成功)/);
    wrapper.unmount();
  });

  it("renders Qoder total points and quota packages separately", async () => {
    routeParams.provider = "qoder";
    routeParams.accountId = "qd-main";
    const qoderAccount = { ...account, provider: "qoder", account_id: "qd-main" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/credentials")) return response({ credentials: [] });
      if (url.includes("/metrics/accounts/")) return response({ snapshots: [{ metric_kind: "quota", status: "fresh", value: { user_quota: { remaining: 80, total: 100, used: 20, unit: "credits", expires_at: "2026-09-01T00:00:00Z" }, add_on_quota: { remaining: 30, total: 50, used: 20, unit: "credits" }, org_resource_package: { remaining: 5, total: 5, used: 0, unit: "credits" } } }] });
      if (url.includes("/usage/events")) return response({ events: [] });
      if (url.includes("/checkin/runs/")) return response({ attempts: [{ provider: "qoder", account_id: "qd-main", outcome: "CLAIMED", reward_credits: 100, reward_expires_at: "2026-09-04T13:55:08Z", finished_at: "2026-08-04T13:55:11Z", quota_after: { packages: [{ name: "user_quota", remaining: 80, total: 100, used: 20, unit: "credits", expires_at: "2026-09-01T00:00:00Z" }, { name: "签到奖励", remaining: 100, total: 100, used: 0, unit: "credits", expires_at: "2026-09-04T13:55:08Z" }] } }] });
      if (url.includes("/checkin/runs?")) return response({ runs: [{ run_id: "run-qoder" }] });
      return response(qoderAccount);
    }));
    const wrapper = mount(AccountDetailPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();
    const summary = wrapper.find(".detail-main-grid .metric-list").text();
    expect(summary).toContain("115 credits");
    expect(summary).not.toContain("配额");
    const table = wrapper.find(".credits-detail-table");
    expect(table.exists()).toBe(true);
    expect(table.text()).toContain("用户积分");
    expect(table.text()).toContain("附加积分");
    expect(table.text()).toContain("组织积分");
    expect(table.text()).toContain("签到奖励");
    wrapper.unmount();
    routeParams.provider = "codebuddy";
    routeParams.accountId = "cb-main";
  });
});
