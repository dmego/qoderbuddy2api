import { VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import ProxyKeysPage from "@/pages/ProxyKeysPage.vue";

describe("ProxyKeysPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("creates and reveals a key once without browser persistence", async () => {
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    let issued = 0;
    const fetchMock = vi.fn(async (_input: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        issued += 1;
        return {
          ok: true,
          json: async () => ({
            key_id: "pk-new",
            key: issued === 1 ? "qb2api_one_time_secret" : "qb2api_replacement_secret",
            name: "Codex",
            expires_at: null,
          }),
        };
      }
      return { ok: true, json: async () => ({ keys: [] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(ProxyKeysPage, {
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });

    await flushPromises();
    await wrapper.get("#proxy-key-name").setValue("Codex");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("qb2api_one_time_secret");
    expect(wrapper.text()).toContain("仅显示这一次");
    expect(storageSpy).not.toHaveBeenCalled();

    await wrapper.get("[data-test='copy-secret']").trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith("qb2api_one_time_secret");
    expect(wrapper.text()).toContain("已复制到剪贴板");

    await wrapper.get("#proxy-key-name").setValue("Codex replacement");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("qb2api_replacement_secret");
    expect(wrapper.text()).not.toContain("已复制到剪贴板");

    await wrapper.get("[data-test='dismiss-secret']").trigger("click");
    expect(wrapper.text()).not.toContain("qb2api_replacement_secret");
    storageSpy.mockRestore();
  });

  it("requires confirmation before revoking a live key", async () => {
    let revoked = false;
    const fetchMock = vi.fn(async (_input: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        revoked = true;
        return {
          ok: true,
          json: async () => ({
            status: "runtime_pending",
            key_id: "pk-live",
            runtime_apply: { status: "failed", error_code: "runtime_reload_failed" },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          keys: [{
            key_id: "pk-live",
            name: "Claude Code",
            scopes: ["proxy"],
            enabled: !revoked,
            created_at: "2026-07-24T00:00:00+00:00",
            last_used_at: null,
            expires_at: null,
            revoked_at: revoked ? "2026-07-24T01:00:00+00:00" : null,
            runtime_apply_status: revoked ? "failed" : "succeeded",
          }],
        }),
      };
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(ProxyKeysPage, {
      global: { plugins: [createPinia(), VueQueryPlugin] },
    });

    await flushPromises();
    await wrapper.get("[data-test='revoke-pk-live']").trigger("click");
    expect(wrapper.text()).toContain("确认撤销 Proxy Key");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await wrapper.get("[data-test='confirm-destructive']").trigger("click");
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(wrapper.text()).toContain("旧 Key 可能仍有效");
    expect(wrapper.text()).toContain("Worker 未同步");
  });
});
