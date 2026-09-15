import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AccountsPage from "@/pages/AccountsPage.vue";
import AddAccountPage from "@/pages/AddAccountPage.vue";
import CheckinPage from "@/pages/CheckinPage.vue";
import CredentialsPage from "@/pages/CredentialsPage.vue";
import CreditsPage from "@/pages/CreditsPage.vue";
import UsagePage from "@/pages/UsagePage.vue";
import { router } from "@/router";
import growthSource from "@/pages/GrowthPage.vue?raw";

const INTL = { value: "workbuddy_intl", label: "WorkBuddy 国际版" };

describe("provider select options", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("appends WorkBuddy 国际版 after CodeBuddy on the accounts page", async () => {
    const wrapper = await mountPage(AccountsPage);
    expect(providerOptions(wrapper)).toEqual(["", "codebuddy", "workbuddy_intl"]);
  });

  it("appends WorkBuddy 国际版 on the credits page", async () => {
    const wrapper = await mountPage(CreditsPage);
    expect(providerOptions(wrapper)).toEqual(["", "codebuddy", "workbuddy_intl"]);
  });

  it("appends WorkBuddy 国际版 on the credentials page", async () => {
    const wrapper = await mountPage(CredentialsPage);
    expect(providerOptions(wrapper)).toEqual(["", "codebuddy", "workbuddy_intl"]);
  });

  it("appends WorkBuddy 国际版 on the usage page", async () => {
    const wrapper = await mountPage(UsagePage);
    expect(providerOptions(wrapper)).toEqual(["", "codebuddy", "workbuddy_intl"]);
  });


  it("never offers WorkBuddy 国际版 on the check-in page", async () => {
    const checkin = await mountPage(CheckinPage);
    expect(providerOptions(checkin)).toEqual(["", "codebuddy"]);
    expect(checkin.text()).not.toContain(INTL.label);
  });

  it("never offers WorkBuddy 国际版 on the growth page", () => {
    // 成长中心依赖 vue-router 的 query，这里只断言源码不引入国际版选项/文案。
    expect(growthSource).not.toContain(INTL.value);
    expect(growthSource).not.toContain(INTL.label);
  });
});

describe("AddAccountPage provider query", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it.each([
    ["codebuddy", "WorkBuddy"],
    ["workbuddy_intl", "WorkBuddy 国际版"],
  ])("honours ?provider=%s instead of defaulting to WorkBuddy", async (requested, expected) => {
    stubSession();
    await router.push({ name: "account-add", query: { provider: requested } });
    await router.isReady();
    const wrapper = mount(AddAccountPage, { global: { plugins: [createPinia(), VueQueryPlugin, router] } });
    await flushPromises();

    const active = wrapper.get(".segmented-control button.active");
    expect(active.text()).toBe(expected);
    wrapper.unmount();
  });

  it("falls back to WorkBuddy for an unknown provider", async () => {
    stubSession();
    await router.push({ name: "account-add", query: { provider: "unknown-provider" } });
    await router.isReady();
    const wrapper = mount(AddAccountPage, { global: { plugins: [createPinia(), VueQueryPlugin, router] } });
    await flushPromises();

    expect(wrapper.get(".segmented-control button.active").text()).toBe("WorkBuddy");
    wrapper.unmount();
  });
});

// 路由守卫会读取 /session 换取 CSRF，否则会重定向到登录页而丢掉 query。
function stubSession(): void {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    return response(url.includes("/session") ? { csrf_token: "test-csrf" } : {});
  }));
}

async function mountPage(component: unknown): Promise<VueWrapper> {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/checkin/runs")) return response({ runs: [], next_cursor: null, limit: 20 });
    if (url.includes("/checkin/status")) return response({ enabled: true, running: false, eligible_accounts: [], daily_states: [] });
    if (url.includes("/usage/summary")) return response({ summary: { request_count: 0, success_count: 0, error_count: 0 } });
    if (url.includes("/usage/timeseries")) return response({ points: [] });
    if (url.includes("/usage/events")) return response({ events: [], next_cursor: null });
    if (url.includes("/metrics/accounts")) return response({ snapshots: [] });
    if (url.includes("/settings")) return response({ settings: [] });
    if (url.includes("/models")) return response({ models: [], next_cursor: null, total: 0 });
    if (url.includes("/accounts")) return response({ accounts: [], next_cursor: null, total: 0 });
    return response({});
  }));
  const wrapper = mount(component as never, { global: { plugins: [createPinia(), VueQueryPlugin] } });
  await flushPromises();
  return wrapper;
}

function providerOptions(wrapper: VueWrapper): string[] {
  const select = wrapper.findAll("select").find((item) => item.findAll("option").some((option) => option.element.value === "codebuddy"));
  if (!select) throw new Error("provider select not found");
  return select.findAll("option").map((option) => option.element.value);
}

function response(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}
