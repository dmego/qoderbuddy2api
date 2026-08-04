import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountsPage from "@/pages/AccountsPage.vue";
import AuditPage from "@/pages/AuditPage.vue";
import AccessibleDrawer from "@/components/AccessibleDrawer.vue";
import StatePill from "@/components/StatePill.vue";
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
    close?.focus();
    drawer!.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }));
    expect(document.activeElement).toBe(drawerButtons.at(-1));
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
    const wrapper = mount(AccountsPage, { attachTo: document.body, global: { plugins: [createPinia(), VueQueryPlugin], stubs: { AccountImportPanel: true } } });
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
    expect(wrapper.get(".table-link").attributes("aria-label")).toBe("查看 研发账号 详情");
    expect(document.querySelector(".detail-drawer")).toBeNull();
    wrapper.unmount();
  });

  it("restores external focus when an open drawer unmounts", async () => {
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const wrapper = mount(AccessibleDrawer, { attachTo: document.body, props: { open: true, title: "详情" } });
    await flushPromises();

    expect(document.activeElement).not.toBe(trigger);
    wrapper.unmount();
    expect(document.activeElement).toBe(trigger);
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

    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/audit?limit=25&cursor=audit-next");
    await wrapper.get('input[aria-label="审计搜索"]').setValue("qd-1");
    await wrapper.get('input[aria-label="动作前缀"]').setValue("account");
    await wrapper.get('select[aria-label="审计类别"]').setValue("account");
    const apply = wrapper.findAll("button").find((button) => button.text().includes("应用"));
    expect(apply?.exists()).toBe(true);
    await apply!.trigger("click");
    await flushPromises();

    expect(wrapper.get('input[aria-label="动作前缀"]').attributes("placeholder")).toBe("例如 account");
    for (const value of ["checkin", "metrics", "model", "proxy_key", "usage"]) expect(wrapper.find(`select[aria-label="审计类别"] option[value="${value}"]`).exists()).toBe(true);
    for (const value of ["checkin", "metrics", "model", "proxy_key", "usage"]) expect(wrapper.find(`select[aria-label="审计资源"] option[value="${value}"]`).exists()).toBe(true);
    for (const value of ["running", "cancelled"]) expect(wrapper.find(`select[aria-label="审计结果"] option[value="${value}"]`).exists()).toBe(true);
    expect(calls).toContain("/api/admin/audit?limit=25&search=qd-1&action_prefix=account&category=account");
    await buttonWithText(wrapper, "下一页").trigger("click");
    await flushPromises();
    expect(calls).toContain("/api/admin/audit?limit=25&cursor=audit-next&search=qd-1&action_prefix=account&category=account");
    wrapper.unmount();
  });

  it.each([
    ["failed", "notification--error", "服务操作失败"],
    ["cancelled", "notification--warning", "服务操作已取消"],
  ])("uses the polled %s terminal status for service feedback", async (status, toneClass, title) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET";
      if (url.includes("/service/events")) return response({ events: [], next_cursor: null });
      if (url.includes("/service/operations/operation-1")) return response({ operation_id: "operation-1", action: "reload", status, error: `${status}_reason` });
      if (method === "POST" && url.endsWith("/service/reload")) return response({ operation_id: "operation-1", action: "reload", status: "running" });
      return response({ service: "proxy-worker", desired_state: "RUNNING", observed_state: "HEALTHY", in_flight: 0 });
    }));
    const wrapper = mountPage(ServicePage);
    await flushPromises();

    await buttonWithText(wrapper, "重载配置").trigger("click");
    await flushPromises();

    const notification = wrapper.find(`.${toneClass}`);
    expect(notification.exists()).toBe(true);
    expect(notification.text()).toContain(title);
    expect(notification.text()).toContain(`${status}_reason`);
    expect(wrapper.text()).not.toContain("服务操作已完成");
    wrapper.unmount();
  });

  it("emits stable hooks for action-required state and the drawer close target", async () => {
    const state = mount(StatePill, { props: { value: "action_required" } });
    const drawer = mount(AccessibleDrawer, { attachTo: document.body, props: { open: true, title: "详情" } });
    await flushPromises();

    expect(state.classes()).toContain("state-pill--action-required");
    expect(document.querySelector(".drawer-close")?.getAttribute("aria-label")).toBe("关闭详情");
    state.unmount();
    drawer.unmount();
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
