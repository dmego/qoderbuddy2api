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
        return response({ events: [{ event_id: "event-q1", provider: "qoder", account_id: "qd-1", model_id: "model-a", protocol: "openai", status: "succeeded", http_status: 200, input_tokens: 4, output_tokens: 3, latency_ms: 120, started_at: "2026-07-23T00:00:00+00:00" }] });
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

    expect(wrapper.text()).toContain("request-q1");
    expect(wrapper.text()).not.toContain("must never render");

    await wrapper.find('[aria-label="Provider"]').setValue("qoder");
    await flushPromises();

    expect(wrapper.find('a[download="usage-events.csv"]').attributes("href")).toContain("provider=qoder");
    expect(calls.some((url) => url.includes("/usage/timeseries") && url.includes("provider=qoder"))).toBe(true);
  });
});

function response(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}
