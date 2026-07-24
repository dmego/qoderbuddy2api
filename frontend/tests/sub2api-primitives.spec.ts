import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it } from "vitest";

import BaseDialog from "@/components/sub2api/feedback/BaseDialog.vue";
import ConfirmDialog from "@/components/sub2api/feedback/ConfirmDialog.vue";
import StatusBadge from "@/components/sub2api/feedback/StatusBadge.vue";

describe("Sub2API-derived UI primitives", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("renders a status as text and tone instead of color alone", () => {
    const wrapper = mount(StatusBadge, { props: { value: "action_required" } });

    expect(wrapper.text()).toContain("需要处理");
    expect(wrapper.attributes("data-tone")).toBe("warning");
  });

  it("restores focus after a dialog closes", async () => {
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const wrapper = mount(BaseDialog, { attachTo: document.body, props: { open: true, title: "删除账号" } });
    await flushPromises();

    const close = document.querySelector<HTMLButtonElement>('button[aria-label="关闭对话框"]');
    expect(close).not.toBeNull();
    close?.click();
    expect(wrapper.emitted("close")).toHaveLength(1);
    await wrapper.setProps({ open: false });

    expect(document.activeElement).toBe(trigger);
  });

  it("requires typed confirmation for a destructive action", async () => {
    const wrapper = mount(ConfirmDialog, {
      attachTo: document.body,
      props: { open: true, title: "删除账号", description: "该操作不可撤销", tone: "danger", verificationText: "删除" },
    });
    await flushPromises();

    const input = document.querySelector<HTMLInputElement>('input[aria-label="确认文本"]');
    const confirm = document.querySelector<HTMLButtonElement>('button[data-action="confirm"]');
    expect(input).not.toBeNull();
    expect(confirm?.disabled).toBe(true);
    if (input) input.value = "删除";
    input?.dispatchEvent(new Event("input", { bubbles: true }));
    await flushPromises();

    expect(confirm?.disabled).toBe(false);
    wrapper.unmount();
  });
});
