import { VueQueryPlugin } from "@tanstack/vue-query";
import { mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import AdminShell from "@/layouts/AdminShell.vue";
import { router } from "@/router";

describe("AdminShell", () => {
  afterEach(() => {
    document.documentElement.classList.remove("dark");
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("uses the source-derived shell for theme and desktop collapse", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    await router.push("/overview");
    await router.isReady();
    const wrapper = mount(AdminShell, { global: { plugins: [createPinia(), VueQueryPlugin, router] } });

    const themeToggle = wrapper.get('button[aria-label^="切换主题"]');
    await themeToggle.trigger("click");
    await themeToggle.trigger("click");
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    await wrapper.get('button[aria-label="收起导航"]').trigger("click");
    expect(wrapper.get('[data-testid="admin-sidebar"]').classes()).toContain("lg:w-[72px]");
  });
});
