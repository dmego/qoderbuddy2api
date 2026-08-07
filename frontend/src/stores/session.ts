import { defineStore } from "pinia";
import { computed, ref } from "vue";

export const useSessionStore = defineStore("session", () => {
  const csrfToken = ref<string | null>(null);
  const authenticated = computed(() => csrfToken.value !== null);

  function establish(token: string): void {
    csrfToken.value = token;
  }

  function isAuthenticated(): boolean {
    return csrfToken.value !== null;
  }

  function clear(): void {
    csrfToken.value = null;
  }

  return { authenticated, csrfToken, establish, clear, isAuthenticated };
});
