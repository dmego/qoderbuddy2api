import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { VueQueryPlugin } from "@tanstack/vue-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import CredentialsPage from "@/pages/CredentialsPage.vue";

function response(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** An OrcaTerm credential as the backend reports it: renewable, no refresh_token field. */
function orcatermRow(overrides: Record<string, unknown> = {}) {
  const issued = Date.now();
  return {
    provider: "orcaterm",
    account_id: "oct-1",
    purpose: "chat",
    mode: "bearer",
    payload_version: 1,
    credential_version: 6,
    expires_at: new Date(issued + 2 * 3600_000).toISOString(),
    updated_at: new Date(issued).toISOString(),
    has_refresh_token: true,
    ...overrides,
  };
}

async function mountWith(rows: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/credentials")) return response({ credentials: rows });
      if (url.includes("/session")) return response({ csrf_token: "test-csrf" });
      return response({});
    }),
  );
  const wrapper = mount(CredentialsPage, {
    global: { plugins: [createPinia(), VueQueryPlugin] },
  });
  await flushPromises();
  return wrapper;
}

describe("credentials page refresh + expiry reporting", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("reports a renewable credential as refreshable even without a refresh_token field", async () => {
    // OrcaTerm renews with the access token itself, so the row has no
    // refresh_token column yet rotation works. Showing "无 refresh" here told
    // the operator the opposite of the truth and invited a pointless re-login.
    const wrapper = await mountWith([orcatermRow()]);
    expect(wrapper.text()).toContain("可刷新");
    expect(wrapper.text()).not.toContain("无 refresh");
  });

  it("does not call a freshly rotated short-lived token expiring", async () => {
    // A 2h token is always inside a fixed 7-day window, so a just-rotated
    // credential used to read as "即将过期" immediately after being renewed.
    const wrapper = await mountWith([orcatermRow()]);
    const row = wrapper.find("tbody tr");
    expect(row.text()).toContain("有效");
    expect(row.text()).not.toContain("即将过期");
  });

  it("still flags a credential that is genuinely near the end of its life", async () => {
    const issued = Date.now() - 2 * 3600_000;
    const wrapper = await mountWith([
      orcatermRow({
        expires_at: new Date(issued + 2 * 3600_000 + 60_000).toISOString(),
        updated_at: new Date(issued).toISOString(),
      }),
    ]);
    expect(wrapper.find("tbody tr").text()).toContain("即将过期");
  });

  it("reports an elapsed credential as expired rather than valid", async () => {
    const issued = Date.now() - 3 * 3600_000;
    const wrapper = await mountWith([
      orcatermRow({
        expires_at: new Date(Date.now() - 60_000).toISOString(),
        updated_at: new Date(issued).toISOString(),
      }),
    ]);
    expect(wrapper.find("tbody tr").text()).toContain("已过期");
  });

  it("keeps long-lived credentials valid without a special case", async () => {
    const issued = Date.now();
    const wrapper = await mountWith([
      orcatermRow({
        provider: "workbuddy_intl",
        expires_at: new Date(issued + 364 * 86_400_000).toISOString(),
        updated_at: new Date(issued).toISOString(),
        has_refresh_token: true,
      }),
    ]);
    const row = wrapper.find("tbody tr");
    expect(row.text()).toContain("有效");
    expect(row.text()).toContain("可刷新");
  });
});
