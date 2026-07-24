import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountsPage from "@/pages/AccountsPage.vue";
import AuditPage from "@/pages/AuditPage.vue";
import ModelsPage from "@/pages/ModelsPage.vue";
import ServicePage from "@/pages/ServicePage.vue";

describe("operations API contracts", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("uses service event filters and preserves them on the next cursor page", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); calls.push(url);
      if (url.includes("/service/events")) return response({
        events: [{ event_id: "event-1", event_type: "state", status: "failed", in_flight: 3, error_code: "worker_state_error", created_at: "2026-07-24T00:00:00Z" }],
        next_cursor: url.includes("cursor=service-next") ? null : "service-next",
      });
      return response({ service: "proxy-worker", desired_state: "RUNNING", observed_state: "HEALTHY", in_flight: 3 });
    }));
    const wrapper = mountPage(ServicePage);
    await flushPromises();

    await wrapper.get('select[aria-label="筛选事件类型"]').setValue("state");
    await wrapper.get('select[aria-label="筛选事件状态"]').setValue("failed");
    await flushPromises();

    expect(calls).toContain("/api/admin/service/events?limit=20&event_type=state&status=failed");
    expect(wrapper.text()).toContain("worker_state_error");
    expect(wrapper.text()).toContain("3");
    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/service/events?limit=20&cursor=service-next&event_type=state&status=failed");
    wrapper.unmount();
  });

  it("uses model query filters, cursor pagination, and an accessible drawer", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); calls.push(url);
      if (url.includes("/usage/summary")) return response({ summary: { request_count: 5, success_count: 4, error_count: 1, latency_avg_ms: 120, latency_p95_ms: 240 } });
      return response({ models: [model], next_cursor: url.includes("cursor=model-next") ? null : "model-next" });
    }));
    const wrapper = mountPage(ModelsPage);
    await flushPromises();

    await wrapper.get(".filter-search input").setValue("model-a");
    await wrapper.get(".filter-grid select").setValue("qoder");
    await buttonWithText(wrapper, "应用").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/models?limit=20&query=model-a&provider=qoder");

    const trigger = wrapper.get(".table-link");
    (trigger.element as HTMLElement).focus();
    await trigger.trigger("click");
    await flushPromises();
    const drawer = document.querySelector<HTMLElement>('[role="dialog"][aria-labelledby]');
    const close = document.querySelector<HTMLButtonElement>('button[aria-label="关闭模型详情"]');
    expect(drawer).not.toBeNull();
    expect(document.activeElement).toBe(close);
    expect(document.body.textContent).toContain("240 ms");
    const drawerButtons = [...drawer!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
    drawerButtons.at(-1)?.focus();
    drawer!.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(close);
    drawer!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(document.activeElement).toBe(trigger.element);

    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/models?limit=20&cursor=model-next&query=model-a&provider=qoder");
    wrapper.unmount();
  });

  it("uses account query and only exposes supported provider/status values", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); calls.push(url);
      if (url.includes("/metrics/accounts")) return response({ snapshots: [] });
      return response({ accounts: [account], next_cursor: url.includes("cursor=account-next") ? null : "account-next" });
    }));
    const wrapper = mount(AccountsPage, { global: { plugins: [createPinia(), VueQueryPlugin], stubs: { AccountImportPanel: true } } });
    await flushPromises();

    await wrapper.get(".filter-search input").setValue("研发");
    const selects = wrapper.findAll(".filter-grid select");
    await selects[0].setValue("qoder");
    await selects[2].setValue("action_required");
    await selects[3].setValue("chat");
    await buttonWithText(wrapper, "应用").trigger("click");
    await flushPromises();

    expect(wrapper.find('option[value="workbuddy"]').exists()).toBe(false);
    expect(wrapper.find('option[value="needs_reauth"]').exists()).toBe(false);
    expect(calls).toContain("/api/admin/accounts?limit=20&query=%E7%A0%94%E5%8F%91&provider=qoder&status=action_required&purpose=chat");
    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/accounts?limit=20&cursor=account-next&query=%E7%A0%94%E5%8F%91&provider=qoder&status=action_required&purpose=chat");
    wrapper.unmount();
  });

  it("uses audit query, action prefix, category, and cursor contracts", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); calls.push(url);
      if (url.includes("/backup")) return response({ backups: [] });
      return response({ events: [{ event_id: "audit-1", action: "account.refresh", resource_type: "account", result: "succeeded", created_at: "2026-07-24T00:00:00Z" }], next_cursor: url.includes("cursor=audit-next") ? null : "audit-next" });
    }));
    const wrapper = mountPage(AuditPage);
    await flushPromises();

    await wrapper.get('input[aria-label="审计搜索"]').setValue("qd-1");
    await wrapper.get('input[aria-label="动作前缀"]').setValue("account.");
    await wrapper.get('select[aria-label="审计类别"]').setValue("account");
    await flushPromises();

    expect(calls).toContain("/api/admin/audit?limit=25&query=qd-1&action_prefix=account.&category=account");
    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/audit?limit=25&cursor=audit-next&query=qd-1&action_prefix=account.&category=account");
    wrapper.unmount();
  });
});

const model = { provider: "qoder", model_id: "model-a", display_name: "Model A", capabilities: ["chat"], source: "definition", enabled: true };
const account = { provider: "qoder", account_id: "qd-1", label: "研发账号", source: "manual", enabled: true, summary_status: "action_required", purposes: { chat: { enabled: true, status: "active", verification_status: "verified" } } };

function mountPage(component: Parameters<typeof mount>[0]) {
  return mount(component, { attachTo: document.body, global: { plugins: [createPinia(), VueQueryPlugin] } });
}

function buttonWithText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(text));
  if (!button) throw new Error(`Button not found: ${text}`);
  return button;
}

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}
