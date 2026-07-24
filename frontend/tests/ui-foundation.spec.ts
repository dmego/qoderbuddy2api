import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it } from "vitest";

import { useUiStore } from "@/stores/ui";

describe("useUiStore", () => {
  afterEach(() => {
    document.documentElement.classList.remove("dark");
    localStorage.clear();
  });

  it("applies explicit dark theme without persisting credentials", () => {
    setActivePinia(createPinia());
    const ui = useUiStore();

    ui.setTheme("dark");

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("2api-ui-theme")).toBe("dark");
    expect(Object.keys(localStorage)).not.toContain("admin-key");
  });
});
