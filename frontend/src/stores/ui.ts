/* SPDX-License-Identifier: LGPL-3.0-or-later
 * Derived from Wei-Shaw/sub2api frontend component theme behavior at cb24522.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

export type Theme = "light" | "dark" | "system";

const THEME_STORAGE_KEY = "2api-ui-theme";

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  } catch {
    return "system";
  }
}

function prefersDark(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches
    : false;
}

function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && prefersDark()));
}

function persistTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 浏览器私密模式或受限嵌入环境不应阻止控制台使用。
  }
}

export const useUiStore = defineStore("ui", () => {
  const theme = ref<Theme>(readTheme());
  const navigationCollapsed = ref(false);
  const mobileNavigationOpen = ref(false);

  function setTheme(next: Theme): void {
    theme.value = next;
    persistTheme(next);
    applyTheme(next);
  }

  function initializeTheme(): void {
    applyTheme(theme.value);
  }

  function toggleNavigation(): void {
    mobileNavigationOpen.value = !mobileNavigationOpen.value;
  }

  function closeMobileNavigation(): void {
    mobileNavigationOpen.value = false;
  }

  return { theme, navigationCollapsed, mobileNavigationOpen, setTheme, initializeTheme, toggleNavigation, closeMobileNavigation };
});
