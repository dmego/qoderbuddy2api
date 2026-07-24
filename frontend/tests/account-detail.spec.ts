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

    await wrapper.findAll("button").find((button) => button.text().includes("验证签到"))?.trigger("click");
    expect(document.body.textContent).toContain("可能立即领取当天积分");
    expect(calls.some((call) => call.url.endsWith("/verify-checkin"))).toBe(false);
    document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)")?.click();
    await flushPromises();

    expect(calls).toContainEqual({ url: "/api/admin/accounts/codebuddy/cb-main/verify-checkin", method: "POST" });
    wrapper.unmount();
  });
});
