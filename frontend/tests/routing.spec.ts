import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import RoutingPage from "@/pages/RoutingPage.vue";

type Call = { url: string; method: string; body?: unknown };

const models = [
  {
    model_id: "deepseek-v4.1-flash",
    name: "DeepSeek V4.1 Flash",
    configured: false,
    routes: [
      {
        provider: "codebuddy", priority: 0, weight: 1, enabled: true, capabilities: ["chat", "streaming"],
        accounts: [
          { account_id: "cb-1", label: "主账号", source: "manual", blocked: false, block: null },
        ],
      },
      {
        provider: "workbuddy_intl", priority: 0, weight: 1, enabled: true, capabilities: ["chat", "streaming"],
        accounts: [
          { account_id: "wbintl-1", label: "国际号", source: "oauth", blocked: false, block: null },
        ],
      },
    ],
  },
  {
    model_id: "hy3",
    name: "HY3",
    configured: true,
    routes: [
      {
        provider: "codebuddy", priority: 2, weight: 1, enabled: true, capabilities: ["chat"],
        accounts: [
          {
            account_id: "cb-2", label: "备用号", source: "manual", blocked: true,
            block: {
              source: "auto",
              reason: "您的使用量已超出频率限制",
              blocked_until: "2026-09-11T10:00:47Z",
            },
          },
        ],
      },
      {
        provider: "workbuddy_intl", priority: 0, weight: 3, enabled: true, capabilities: ["chat"],
        accounts: [],
      },
    ],
  },
];

describe("RoutingPage", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("lists models collapsed by default and expands on demand", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();

    expect(wrapper.findAll(".routing-model")).toHaveLength(2);
    // Collapsed rows still summarise the policy, so the page reads as a compact list.
    expect(wrapper.text()).toContain("DeepSeek V4.1 Flash");
    expect(wrapper.text()).toContain("CodeBuddy P0/W1");
    // No editable inputs until a row is expanded.
    expect(wrapper.find('input[aria-label="deepseek-v4.1-flash codebuddy 优先级"]').exists()).toBe(false);

    await wrapper.get(".routing-model__head").trigger("click");
    await flushPromises();
    expect(wrapper.find('input[aria-label="deepseek-v4.1-flash codebuddy 优先级"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("expands and collapses every visible model from the header actions", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();

    await buttonWithText(wrapper, "全部展开").trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".routing-model__body")).toHaveLength(2);

    await buttonWithText(wrapper, "全部收起").trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".routing-model__body")).toHaveLength(0);
    wrapper.unmount();
  });

  it("explains the fields behind a toggle instead of always occupying space", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();

    expect(wrapper.text()).not.toContain("数字越小越先尝试");
    await buttonWithText(wrapper, "字段说明").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("数字越小越先尝试");
    expect(wrapper.text()).toContain("按「账号 × 模型」停用");
    wrapper.unmount();
  });

  it("saves an edited priority and weight through PUT /routing/{model_id}", async () => {
    const calls = stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 0);

    await wrapper.get('input[aria-label="deepseek-v4.1-flash workbuddy_intl 优先级"]').setValue(0);
    await wrapper.get('input[aria-label="deepseek-v4.1-flash codebuddy 优先级"]').setValue(1);
    await wrapper.get('input[aria-label="deepseek-v4.1-flash workbuddy_intl 权重"]').setValue(4);
    await buttonWithText(wrapper, "保存").trigger("click");
    await flushPromises();

    const put = calls.find((call) => call.method === "PUT");
    expect(put?.url).toBe("/api/admin/routing/deepseek-v4.1-flash");
    expect(put?.body).toEqual({
      routes: [
        { provider: "codebuddy", priority: 1, weight: 1, enabled: true },
        { provider: "workbuddy_intl", priority: 0, weight: 4, enabled: true },
      ],
    });
    wrapper.unmount();
  });

  it("applies the 国际版优先 preset: lowest priority for workbuddy_intl and saves immediately", async () => {
    const calls = stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 0);

    await buttonWithText(wrapper, "国际版优先").trigger("click");
    await flushPromises();

    const put = calls.find((call) => call.method === "PUT");
    expect(put?.url).toBe("/api/admin/routing/deepseek-v4.1-flash");
    expect(put?.body).toEqual({
      routes: [
        { provider: "codebuddy", priority: 1, weight: 1, enabled: true },
        { provider: "workbuddy_intl", priority: 0, weight: 1, enabled: true },
      ],
    });
    wrapper.unmount();
  });

  it("blocks saving a policy that would leave no usable route", async () => {
    const calls = stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 0);

    await wrapper.get('input[aria-label="deepseek-v4.1-flash codebuddy 启用"]').setValue(false);
    await wrapper.get('input[aria-label="deepseek-v4.1-flash workbuddy_intl 启用"]').setValue(false);
    await flushPromises();

    expect(wrapper.text()).toContain("至少要保留一条启用的路线");
    await buttonWithText(wrapper, "保存").trigger("click");
    await flushPromises();
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
    wrapper.unmount();
  });

  it("resets a custom policy through DELETE after confirmation", async () => {
    const calls = stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 1);

    await buttonWithText(wrapper, "恢复默认").trigger("click");
    await flushPromises();
    expect(document.body.textContent).toContain("恢复默认轮询");

    document.querySelector<HTMLButtonElement>(".dialog-actions button:not(.secondary-button)")?.click();
    await flushPromises();

    expect(calls.some((call) => call.method === "DELETE" && call.url === "/api/admin/routing/hy3")).toBe(true);
    wrapper.unmount();
  });

  it("filters the model list by search text and configured state", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();

    await wrapper.get('input[aria-label="模型名称或 ID"]').setValue("hy3");
    await buttonWithText(wrapper, "应用").trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".routing-model")).toHaveLength(1);
    expect(wrapper.text()).toContain("HY3");

    await wrapper.get('input[aria-label="仅已自定义"]').setValue(true);
    await flushPromises();
    expect(wrapper.findAll(".routing-model")).toHaveLength(1);

    await wrapper.get('input[aria-label="模型名称或 ID"]').setValue("deepseek");
    await buttonWithText(wrapper, "应用").trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".routing-model")).toHaveLength(0);
    wrapper.unmount();
  });

  it("lists every account serving the model, with a checkbox per account", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 0);

    const accounts = wrapper.findAll(".routing-account");
    expect(accounts).toHaveLength(2);
    expect(wrapper.text()).toContain("cb-1");
    expect(wrapper.text()).toContain("wbintl-1");
    // Unblocked accounts render as checked.
    const box = wrapper.get('input[aria-label="deepseek-v4.1-flash cb-1 参与路由"]');
    expect((box.element as HTMLInputElement).checked).toBe(true);
    wrapper.unmount();
  });

  it("blocks one account for one model through the account endpoint", async () => {
    const calls = stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 0);

    await wrapper.get('input[aria-label="deepseek-v4.1-flash cb-1 参与路由"]').setValue(false);
    await flushPromises();

    const put = calls.find((call) => call.method === "PUT" && call.url.includes("/accounts/"));
    expect(put?.url).toBe("/api/admin/routing/deepseek-v4.1-flash/accounts/codebuddy/cb-1");
    expect(put?.body).toEqual({ blocked: true, reason: "管理员手动停用" });
    wrapper.unmount();
  });

  it("unblocks an account and clears the reason through the same endpoint", async () => {
    const calls = stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 1);

    await wrapper.get('input[aria-label="hy3 cb-2 参与路由"]').setValue(true);
    await flushPromises();

    const put = calls.find((call) => call.method === "PUT" && call.url.includes("/accounts/"));
    expect(put?.url).toBe("/api/admin/routing/hy3/accounts/codebuddy/cb-2");
    expect(put?.body).toEqual({ blocked: false, reason: "" });
    wrapper.unmount();
  });

  it("shows why an auto-blocked account is unavailable, including the reset time", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 1);

    const blocked = wrapper.get(".routing-account--blocked");
    expect(blocked.text()).toContain("cb-2");
    expect(blocked.text()).toContain("您的使用量已超出频率限制");
    expect(blocked.text()).toContain("恢复");
    // The collapsed summary surfaces the count so it is visible without expanding.
    expect(wrapper.text()).toContain("1 账号停用");
    wrapper.unmount();
  });

  it("says so when a provider has no usable account", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();
    await expand(wrapper, 1);

    expect(wrapper.text()).toContain("暂无可用账号");
    wrapper.unmount();
  });
});

async function expand(wrapper: VueWrapper, index: number): Promise<void> {
  await wrapper.findAll(".routing-model__head")[index].trigger("click");
  await flushPromises();
}

function stubRouting(): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (method === "DELETE") return response({ status: "reset", model_id: "hy3" });
    if (method === "PUT") return response({ status: "ok", model_id: "deepseek-v4.1-flash", routes: [] });
    return response({ models });
  }));
  return calls;
}

function mountRouting(): VueWrapper {
  return mount(RoutingPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
}

function buttonWithText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll("button").find((item) => item.text().includes(text));
  if (!button) throw new Error(`button not found: ${text}`);
  return button;
}

function response(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}


  it("aligns the model name to the left (regression: button centering)", async () => {
    stubRouting();
    const wrapper = mountRouting();
    await flushPromises();

    // The global button rule centres content, so we have to opt out explicitly.
    const head = wrapper.get(".routing-model__head");
    const style = getComputedStyle(head.element);
    expect(style.justifyContent).not.toBe("center");
    wrapper.unmount();
  });
