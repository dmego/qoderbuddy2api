import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import CreditsPage from "@/pages/CreditsPage.vue";

describe("CreditsPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders current credits, aggregates history, and filters by provider", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); calls.push(url);
      if (url.includes("/metrics/accounts?")) return response({ snapshots: [metric("codebuddy", "cb-1", 120), quotaMetric("qd-1", 80)] });
      if (url.includes("/accounts?")) return response({ accounts: [account("codebuddy", "cb-1", "主账号"), account("qoder", "qd-1", "备用账号")] });
      if (url.includes("/metrics/accounts/codebuddy/cb-1/history")) return response({ rows: [history("2026-08-01T00:00:00Z", 100), history("2026-08-02T00:00:00Z", 120)] });
      if (url.includes("/metrics/accounts/qoder/qd-1/history")) return response({ rows: [quotaHistory("2026-08-01T00:00:00Z", 90), quotaHistory("2026-08-02T00:00:00Z", 80)] });
      return response({});
    }));
    const wrapper = mount(CreditsPage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { MetricChart: { template: "<div data-testid='chart' />" } } } });
    await flushPromises();

    expect(calls).toContain("/api/admin/accounts?limit=100");
    expect(wrapper.text()).toContain("200");
    expect(wrapper.text()).toContain("主账号");
    expect(wrapper.text()).toContain("+20");
    await wrapper.find('[aria-label="服务提供方"]').setValue("codebuddy");
    await flushPromises();
    expect(wrapper.text()).toContain("主账号");
    expect(wrapper.text()).not.toContain("备用账号");
    expect(calls.some((url) => url.includes("provider=codebuddy"))).toBe(true);
  });

  it("keeps unknown current credits unavailable instead of zero", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/metrics/accounts?")) return response({ snapshots: [{ ...metric("codebuddy", "cb-1", 0), value: null, status: "unknown" }] });
      if (url.includes("/accounts?")) return response({ accounts: [account("codebuddy", "cb-1", "无积分账号")] });
      if (url.includes("/history")) return response({ rows: [] });
      return response({});
    }));
    const wrapper = mount(CreditsPage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { MetricChart: true } } });
    await flushPromises();
    expect(wrapper.text()).toContain("--");
    expect(wrapper.text()).toContain("无积分账号");
  });

  it("includes Qoder add-on quota in the current balance", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/metrics/accounts?")) return response({ snapshots: [{ provider: "qoder", account_id: "qd-1", metric_kind: "quota", status: "fresh", observed_at: "2026-08-02T00:00:00Z", value: { user_quota: { remaining: 0 }, add_on_quota: { remaining: 4948 } } }] });
      if (url.includes("/accounts?")) return response({ accounts: [account("qoder", "qd-1", "Qoder")] });
      if (url.includes("/history")) return response({ rows: [] });
      return response({});
    }));
    const wrapper = mount(CreditsPage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { MetricChart: true } } });
    await flushPromises();
    expect(wrapper.text()).toContain("4,948");
  });
});

function account(provider: string, accountId: string, label: string) { return { provider, account_id: accountId, label, enabled: true, summary_status: "active" }; }
function metric(provider: string, accountId: string, remaining: number) { return { provider, account_id: accountId, metric_kind: "points", status: "fresh", observed_at: "2026-08-02T00:00:00Z", value: { total_remaining: remaining, unit: "credits" } }; }
function quotaMetric(accountId: string, remaining: number) { return { provider: "qoder", account_id: accountId, metric_kind: "quota", status: "fresh", observed_at: "2026-08-02T00:00:00Z", value: { user_quota: { remaining, unit: "credits" } } }; }
function history(observed_at: string, total_remaining: number) { return { observed_at, status: "fresh", value: { total_remaining } }; }
function quotaHistory(observed_at: string, remaining: number) { return { observed_at, status: "fresh", value: { user_quota: { remaining, unit: "credits" } } }; }
function response(body: unknown): Response { return { ok: true, json: async () => body } as Response; }
