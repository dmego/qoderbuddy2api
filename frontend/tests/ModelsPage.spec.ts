import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "@/api/client";
import ModelsPage from "@/pages/ModelsPage.vue";

vi.mock("@/api/client", () => ({
  apiRequest: vi.fn().mockResolvedValue({ status: "succeeded", added: 2, updated: 1, disabled: 0, models: [] }),
}));

describe("ModelsPage sync button", () => {
  it("syncs every provider when no provider filter is selected", async () => {
    const wrapper = mount(ModelsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    const button = wrapper.find("button[aria-label='从上游同步']");
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    await flushPromises();

    expect(apiRequest).toHaveBeenCalledWith("/models/sync", { method: "POST" });
  });

  it("syncs only the selected provider when a filter is applied", async () => {
    const wrapper = mount(ModelsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();
    await wrapper.find("select").setValue("codebuddy");
    await flushPromises();
    vi.mocked(apiRequest).mockClear();

    await wrapper.find("button[aria-label='从上游同步']").trigger("click");
    await flushPromises();

    expect(apiRequest).toHaveBeenCalledWith("/models/sync/codebuddy", { method: "POST" });
  });

});

// 能力列必须显示能力名（对话/流式…），而不是布尔值。后端返回对象时
// v-for 会遍历它的值，页面上就会出现 true/false — 这个测试锁住该契约。
describe("ModelsPage capability column", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders capability names as tags and shows 未声明 when empty", async () => {
    vi.mocked(apiRequest).mockResolvedValue({
      models: [
        { model_id: "alpha", display_name: "Alpha", capabilities: ["chat", "streaming", "reasoning"], source: "definition", enabled: true, routes: [{ provider: "codebuddy", upstream_id: "alpha", enabled: true }] },
        { model_id: "beta", display_name: "Beta", capabilities: [], source: "definition", enabled: true, routes: [{ provider: "codebuddy", upstream_id: "beta", enabled: true }] },
      ],
      total: 2,
      next_cursor: null,
    });
    const wrapper = mount(ModelsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    const text = wrapper.text();
    expect(text).toContain("chat");
    expect(text).toContain("streaming");
    expect(text).toContain("reasoning");
    expect(text).toContain("未声明");
    // 绝不能把能力渲染成布尔值。
    expect(text).not.toMatch(/\btrue\b/);
    expect(text).not.toMatch(/\bfalse\b/);
    vi.mocked(apiRequest).mockReset();
  });
});
