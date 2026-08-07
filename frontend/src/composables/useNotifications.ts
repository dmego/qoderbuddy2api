import { onBeforeUnmount, ref } from "vue";

export type NotificationTone = "success" | "error" | "warning" | "info";

export type ConsoleNotification = {
  id: number;
  title: string;
  message?: string;
  tone: NotificationTone;
};

let notificationId = 0;

export function useNotifications() {
  const notifications = ref<ConsoleNotification[]>([]);
  const timers = new Map<number, number>();

  function dismiss(id: number): void {
    notifications.value = notifications.value.filter((item) => item.id !== id);
    const timer = timers.get(id);
    if (timer !== undefined) window.clearTimeout(timer);
    timers.delete(id);
  }

  function notify(
    title: string,
    options: { message?: string; tone?: NotificationTone; timeout?: number } = {},
  ): number {
    const id = ++notificationId;
    notifications.value.push({
      id,
      title,
      message: options.message,
      tone: options.tone ?? "info",
    });
    const timeout = options.timeout ?? (options.tone === "error" ? 8000 : 4500);
    if (timeout > 0) timers.set(id, window.setTimeout(() => dismiss(id), timeout));
    return id;
  }

  onBeforeUnmount(() => {
    for (const timer of timers.values()) window.clearTimeout(timer);
    timers.clear();
  });

  return { notifications, notify, dismiss };
}
