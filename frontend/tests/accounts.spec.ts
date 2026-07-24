import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountsPage from "@/pages/AccountsPage.vue";

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

    expect(wrapper.text()).toContain("已使用 37%");
    expect(wrapper.text()).toContain("接口协议尚未验证");
    expect(wrapper.text()).not.toContain("protocol_not_verified");
  });
});
