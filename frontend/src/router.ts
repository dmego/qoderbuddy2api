import { createRouter, createWebHistory } from "vue-router";
import { getActivePinia } from "pinia";

import AdminShell from "@/layouts/AdminShell.vue";
import { apiRequest } from "@/api/client";
import { useSessionStore } from "@/stores/session";

const LoginPage = () => import("@/pages/LoginPage.vue");
const OverviewPage = () => import("@/pages/OverviewPage.vue");
const ServicePage = () => import("@/pages/ServicePage.vue");
const AccountsPage = () => import("@/pages/AccountsPage.vue");
const CredentialsPage = () => import("@/pages/CredentialsPage.vue");
const ModelsPage = () => import("@/pages/ModelsPage.vue");
const UsagePage = () => import("@/pages/UsagePage.vue");
const CheckinPage = () => import("@/pages/CheckinPage.vue");
const SettingsPage = () => import("@/pages/SettingsPage.vue");
const AuditPage = () => import("@/pages/AuditPage.vue");
const ProxyKeysPage = () => import("@/pages/ProxyKeysPage.vue");

export const router = createRouter({
  history: createWebHistory("/admin/"),
  routes: [
    { path: "/login", name: "login", component: LoginPage },
    {
      path: "/",
      component: AdminShell,
      children: [
        { path: "", redirect: "/overview" },
        { path: "overview", name: "overview", component: OverviewPage },
        { path: "service", name: "service", component: ServicePage },
        { path: "accounts", name: "accounts", component: AccountsPage },
        { path: "credentials", name: "credentials", component: CredentialsPage },
        { path: "proxy-keys", name: "proxy-keys", component: ProxyKeysPage },
        { path: "models", name: "models", component: ModelsPage },
        { path: "usage", name: "usage", component: UsagePage },
        { path: "checkin", name: "checkin", component: CheckinPage },
        { path: "settings", name: "settings", component: SettingsPage },
        { path: "audit", name: "audit", component: AuditPage },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/overview" },
  ],
});

router.beforeEach(async (to) => {
  if (!getActivePinia()) return true;
  const session = useSessionStore();
  if (to.name === "login") return true;
  if (session.authenticated) return true;
  try {
    const result = await apiRequest<{ csrf_token: string | null }>("/session");
    if (!result.csrf_token) return { name: "login" };
    session.establish(result.csrf_token);
    return true;
  } catch {
    session.clear();
    return { name: "login" };
  }
});
