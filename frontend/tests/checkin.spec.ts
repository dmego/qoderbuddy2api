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

  it("starts a manual batch with its durable operation ID", async () => {
    const calls: Array<{ url: string; method: string }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET"; calls.push({ url, method });
      if (method === "POST") return response({ operation_id: "run-manual", run_id: "run-manual", status: "running" }, 202);
      if (url.includes("/checkin/runs/run-manual")) return response({ run: { run_id: "run-manual", status: "running" }, attempts: [] });
      if (url.includes("/checkin/runs")) return response({ runs: [], next_cursor: null });
      return response({
        enabled: true, running: false, local_date: "2026-07-23", timezone: "Asia/Shanghai",
        checkin_at: "08:00", eligible_accounts: [{ provider: "codebuddy", account_id: "cb-1", label: "主账号", status: "active", verification_status: "verified" }], daily_states: [],
      });
    }));
    const wrapper = mount(CheckinPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    await wrapper.get('input[aria-label="选择 主账号"]').setValue(true);
    await buttonWithText(wrapper, "执行选中账号").trigger("click");
    await confirmDialog();

    expect(calls).toContainEqual(expect.objectContaining({ url: "/api/admin/checkin/run", method: "POST" }));
    expect(wrapper.text()).toContain("run-manual");
    wrapper.unmount();
  });

  it("explains when Qoder has disabled the daily check-in", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/checkin/runs/run-qoder")) {
        return response({ run: { run_id: "run-qoder", status: "finished" }, attempts: [{ provider: "qoder", account_id: "qd-1", outcome: "failed", http_status: 200, error_code: "qoder_checkin_disabled", reward_credits: 100, reward_expires_at: "1970-01-01T00:00:00+00:00", attempts: 1 }] });
      }
      if (url.includes("/checkin/runs")) return response({ runs: [{ run_id: "run-qoder", started_at: "2026-08-06T02:48:00Z", status: "finished", trigger: "catch_up", attempt_count: 1, successful_count: 0 }], next_cursor: null });
      return response({ enabled: true, running: false, local_date: "2026-08-06", timezone: "Asia/Shanghai", checkin_at: "10:30", eligible_accounts: [{ provider: "qoder", account_id: "qd-1", status: "active", verification_status: "verified", last_error: "qoder_checkin_disabled" }], daily_states: [] });
    }));
    const wrapper = mount(CheckinPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    expect(wrapper.text()).toContain("Qoder 签到活动已关闭");
    await wrapper.get('button[aria-label="查看批次 run-qoder"]').trigger("click");
    await flushPromises();
    expect(document.body.textContent).toContain("Qoder 今日签到活动已关闭");
    expect(document.body.textContent).toContain("未发放奖励");
    expect(document.body.textContent).toContain("签到未成功，未记录余额变化");
    expect(document.body.textContent).not.toContain("奖励 100 credits");
    expect(document.body.textContent).not.toContain("1970");
    wrapper.unmount();
  });
});

function buttonWithText(wrapper: ReturnType<typeof mount>, text: string) {
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
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}
