import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { describe, expect, it, vi } from "vitest";

import { apiRequest } from "@/api/client";
import ModelsPage from "@/pages/ModelsPage.vue";

vi.mock("@/api/client", () => ({
  apiRequest: vi.fn().mockResolvedValue({ status: "succeeded", added: 2, updated: 1, disabled: 0, models: [] }),
}));

describe("ModelsPage sync button", () => {
  it("shows sync button and calls endpoint", async () => {
    const wrapper = mount(ModelsPage, { global: { plugins: [createPinia(), VueQueryPlugin] } });
    await flushPromises();

    const button = wrapper.find("button[aria-label='从上游同步']");
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    await flushPromises();

    expect(apiRequest).toHaveBeenCalledWith("/models/sync/qoder", { method: "POST" });
  });
});
