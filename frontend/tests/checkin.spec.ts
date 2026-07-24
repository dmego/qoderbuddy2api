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
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const url = String(input); calls.push(url);
      return {
      ok: true,
      json: async () => url.includes("/checkin/runs") ? {
        runs: [{ run_id: "run-later", started_at: "2026-07-23T00:00:00+00:00", status: "finished", trigger: "manual", attempt_count: 2, successful_count: 1 }],
        limit: 20,
        next_cursor: url.includes("cursor=checkin-next") ? null : "checkin-next",
      } : {
        enabled: true,
        running: false,
        local_date: "2026-07-23",
        timezone: "Asia/Shanghai",
        checkin_at: "08:00",
        eligible_accounts: [],
        daily_states: [],
      },
    } as Response;
    }));
    const wrapper = mount(CheckinPage, {
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });

    await flushPromises();

    expect(wrapper.text()).toContain("run-later");
    expect(wrapper.text()).toContain("1 / 2");
    expect(wrapper.find("pre").exists()).toBe(false);
    await wrapper.get('select[aria-label="筛选触发方式"]').setValue("manual");
    await wrapper.get('select[aria-label="筛选批次状态"]').setValue("finished");
    await flushPromises();
    expect(calls).toContain("/api/admin/checkin/runs?limit=20&status=finished&trigger=manual");

    const next = wrapper.findAll("button").find((button) => button.text().includes("下一页"));
    await next?.trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/checkin/runs?limit=20&cursor=checkin-next&status=finished&trigger=manual");
  });
});
