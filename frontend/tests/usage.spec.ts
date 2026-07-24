import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import OperationStatus from "@/components/OperationStatus.vue";
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
    expect(document.body.textContent).toContain("未提交首块");
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

  it("keeps the rollup operation status scalar and exposes backend counters as detail items", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET";
      if (method === "POST" && url.endsWith("/usage/rollup")) return response({ status: { groups: 3, deleted_events: 2 } });
      if (url.includes("/usage/summary")) return response({ summary: { request_count: 0, input_tokens: 0, output_tokens: 0, success_count: 0, error_count: 0, token_event_count: 0, missing_token_count: 0 } });
      if (url.includes("/usage/events")) return response({ events: [], next_cursor: null });
      return response({ rollups: [] });
    }));
    const wrapper = mount(UsagePage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { MetricChart: true } } });
    await flushPromises();

    await wrapper.findAll("button").find((button) => button.text().includes("重算聚合"))?.trigger("click");
    await flushPromises();

    const operation = wrapper.findComponent(OperationStatus).props("operation") as Record<string, unknown>;
    expect(operation.status).toBe("succeeded");
    expect(operation.result).toEqual({ groups: 3, deleted_events: 2 });
    expect(operation.items).toEqual([
      { key: "groups", label: "聚合桶 3", status: "succeeded" },
      { key: "deleted_events", label: "清理明细 2", status: "succeeded" },
    ]);
    expect(wrapper.text()).not.toContain("[object Object]");
  });

  it.each([
    [true, "已提交首块"],
    [false, "未提交首块"],
    [null, "未知"],
  ])("renders stream_committed=%s as a distinct state", async (streamCommitted, expected) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/usage/events/event-state")) return response({ event_id: "event-state", request_id: "request-state", provider: "qoder", account_id: null, model_id: "model-a", protocol: "openai", status: "succeeded", http_status: 200, input_tokens: null, output_tokens: null, latency_ms: null, stream_committed: streamCommitted, started_at: "2026-07-24T00:00:00Z" });
      if (url.includes("/usage/events")) return response({ events: [{ event_id: "event-state", provider: "qoder", account_id: null, model_id: "model-a", protocol: "openai", status: "succeeded", http_status: 200, input_tokens: null, output_tokens: null, latency_ms: null, started_at: "2026-07-24T00:00:00Z" }], next_cursor: null });
      if (url.includes("/usage/summary")) return response({ summary: { request_count: 1, input_tokens: 0, output_tokens: 0, success_count: 1, error_count: 0, token_event_count: 0, missing_token_count: 1 } });
      return response({ rollups: [] });
    }));
    const wrapper = mount(UsagePage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { MetricChart: true } } });
    await flushPromises();
    await wrapper.get('[data-testid="usage-event-event-state"]').trigger("click");
    await flushPromises();

    expect(document.body.textContent).toContain(expected);
    wrapper.unmount();
  });
});

function response(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}
