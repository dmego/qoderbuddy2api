import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import LoginPage from "@/pages/LoginPage.vue";

const replace = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ replace }),
}));

describe("LoginPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    replace.mockReset();
  });

  it("warns before an administrator uses explicit remote HTTP mode", () => {
    const wrapper = mount(LoginPage, { global: { plugins: [createPinia()] } });

    expect(wrapper.text()).toContain("受信局域网 HTTP");
    expect(wrapper.text()).toContain("禁止暴露到公网");
    expect(wrapper.text()).toContain("密钥不会写入浏览器存储");
  });

  it("establishes the session without persisting the admin key", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ csrf_token: "csrf-token" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const wrapper = mount(LoginPage, { global: { plugins: [createPinia()] } });

    await wrapper.get("#admin-key").setValue("admin-secret");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(storageSpy).not.toHaveBeenCalled();
    expect(replace).toHaveBeenCalledWith("/overview");
    expect((wrapper.get("#admin-key").element as HTMLInputElement).value).toBe("");
    storageSpy.mockRestore();
  });
});
