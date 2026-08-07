import { ref } from "vue";

export function useCursorPager() {
  const cursor = ref<string | null>(null);
  const history = ref<(string | null)[]>([]);
  const page = ref(1);

  function next(nextCursor?: string | null): void {
    if (!nextCursor) return;
    history.value.push(cursor.value);
    cursor.value = nextCursor;
    page.value += 1;
  }

  function previous(): void {
    if (!history.value.length) return;
    cursor.value = history.value.pop() ?? null;
    page.value = Math.max(1, page.value - 1);
  }

  function reset(): void {
    cursor.value = null;
    history.value = [];
    page.value = 1;
  }

  return { cursor, page, canPrevious: history, next, previous, reset };
}

export function appendQuery(path: string, values: Record<string, string | number | null | undefined>): string {
  const [base, existing = ""] = path.split("?", 2);
  const params = new URLSearchParams(existing);
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && String(value).trim()) params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}
