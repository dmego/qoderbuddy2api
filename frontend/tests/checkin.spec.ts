import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import CheckinPage from "@/pages/CheckinPage.vue";

describe("CheckinPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders durable run history instead of raw batch JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string) => ({
      ok: true,
      json: async () => input.includes("/checkin/runs") ? {
        runs: [{ run_id: "run-later", started_at: "2026-07-23T00:00:00+00:00", status: "finished", trigger: "manual", attempt_count: 2, successful_count: 1 }],
        limit: 20,
      } : {
        enabled: true,
        running: false,
        local_date: "2026-07-23",
        timezone: "Asia/Shanghai",
        checkin_at: "08:00",
        eligible_accounts: [],
        daily_states: [],
      },
    })));
    const wrapper = mount(CheckinPage, {
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });

    await flushPromises();

    expect(wrapper.text()).toContain("run-later");
    expect(wrapper.text()).toContain("1 / 2");
    expect(wrapper.find("pre").exists()).toBe(false);
  });
});
