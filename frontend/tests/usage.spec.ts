import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import UsagePage from "@/pages/UsagePage.vue";

describe("UsagePage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("filters telemetry, opens a safe event detail, and preserves filters for export", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/usage/events/event-q1")) {
        return response({
          event_id: "event-q1",
          request_id: "request-q1",
          provider: "qoder",
          account_id: "qd-1",
          model_id: "model-a",
          protocol: "openai",
          status: "succeeded",
          http_status: 200,
          input_tokens: 4,
          output_tokens: 3,
          latency_ms: 120,
          stream_committed: false,
          started_at: "2026-07-23T00:00:00+00:00",
          finished_at: "2026-07-23T00:00:01+00:00",
          error_code: null,
          redacted_error: "must never render",
        });
      }
      if (url.includes("/usage/events")) {
        return response({ events: [{ event_id: "event-q1", provider: "qoder", account_id: "qd-1", model_id: "model-a", protocol: "openai", status: "succeeded", http_status: 200, input_tokens: 4, output_tokens: 3, latency_ms: 120, started_at: "2026-07-23T00:00:00+00:00" }], next_cursor: url.includes("cursor=usage-next") ? null : "usage-next" });
      }
      if (url.includes("/usage/summary")) {
        return response({ summary: { request_count: 1, input_tokens: 4, output_tokens: 3, success_count: 1, error_count: 0, token_event_count: 1, missing_token_count: 0 } });
      }
      return response({ rollups: [] });
    }));

    const wrapper = mount(UsagePage, {
      global: {
        plugins: [createPinia(), VueQueryPlugin],
        stubs: { MetricChart: { template: "<div data-testid=\"chart\" />" } },
      },
    });
    await flushPromises();

    expect(wrapper.find("label").text()).toContain("Provider");
    expect(wrapper.find('[data-testid="usage-event-event-q1"]').exists()).toBe(true);

    await wrapper.find('[data-testid="usage-event-event-q1"]').trigger("click");
    await flushPromises();

    expect(document.body.textContent).toContain("request-q1");
    expect(document.body.textContent).not.toContain("must never render");

    await wrapper.find('[aria-label="Provider"]').setValue("qoder");
    await wrapper.find('[aria-label="请求状态"]').setValue("failed");
    await flushPromises();

    expect(wrapper.find('a[download="usage-events.csv"]').attributes("href")).toContain("provider=qoder");
    expect(wrapper.find('option[value="workbuddy"]').exists()).toBe(false);
    expect(calls).toContain("/api/admin/usage/summary?provider=qoder&status=failed");
    expect(calls).toContain("/api/admin/usage/timeseries?bucket_kind=minute&limit=60&provider=qoder&status=failed");
    expect(calls).toContain("/api/admin/usage/events?provider=qoder&status=failed&limit=25");

    const next = wrapper.findAll("button").find((button) => button.text().includes("下一页"));
    await next?.trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/usage/events?provider=qoder&status=failed&limit=25&cursor=usage-next");
  });
});

function response(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}
