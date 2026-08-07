import { mount } from "@vue/test-utils";
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

    for (const label of ["代理服务", "账号", "积分监控", "凭据", "代理密钥", "模型", "用量", "签到", "设置", "审计"]) {
      expect(wrapper.text()).toContain(label);
    }
    expect(wrapper.text()).toContain("多账号代理控制台");
    expect(wrapper.text()).not.toContain("Multi-account gateway");
  });
});
