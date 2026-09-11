import { mount, flushPromises } from "@vue/test-utils";
import { VueQueryPlugin } from "@tanstack/vue-query";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AdminShell from "@/layouts/AdminShell.vue";
import { router } from "@/router";

describe("AdminShell", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("exposes every approved management domain", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    }));
    await router.push("/overview");
    await router.isReady();
    const wrapper = mount(AdminShell, {
      global: { plugins: [createPinia(), VueQueryPlugin, router] },
    });

    for (const label of ["代理服务", "账号", "积分监控", "凭据", "代理密钥", "模型", "路由策略", "用量", "签到", "设置", "审计"]) {
      expect(wrapper.text()).toContain(label);
    }
    expect(wrapper.text()).toContain("多账号代理控制台");
    expect(wrapper.text()).not.toContain("Multi-account gateway");
  });

  it("mounts the routing policy page at /routing", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes("/session") ? { csrf_token: "test-csrf" } : { models: [] };
      return { ok: true, status: 200, json: async () => body } as Response;
    }));
    await router.push("/routing");
    await router.isReady();
    const wrapper = mount(AdminShell, {
      global: { plugins: [createPinia(), VueQueryPlugin, router] },
    });
    await flushPromises();

    expect(router.currentRoute.value.name).toBe("routing");
    expect(wrapper.text()).toContain("路由策略");
    // Field help lives behind a toggle so the page stays compact by default.
    expect(wrapper.text()).toContain("字段说明");
    wrapper.unmount();
  });
});
