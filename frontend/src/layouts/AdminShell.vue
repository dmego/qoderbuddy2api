<script setup lang="ts">
import {
  Activity,
  BookKey,
  Boxes,
  ChartNoAxesCombined,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleGauge,
  ClipboardList,
  Coins,
  DatabaseBackup,
  KeyRound,
  Menu,
  Power,
  Settings,
  UsersRound,
  LogOut,
  X,
} from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { RouterLink, RouterView, useRouter } from "vue-router";
import { onBeforeUnmount, onMounted, ref } from "vue";

import { apiRequest } from "@/api/client";
import { useSessionStore } from "@/stores/session";
import { statusLabel } from "@/utils/presentation";

const navigationGroups = [
  {
    label: "运行",
    items: [
      { to: "/overview", label: "总览", icon: CircleGauge },
      { to: "/service", label: "代理服务", icon: Power },
    ],
  },
  {
    label: "账号池",
    items: [
      { to: "/accounts", label: "账号", icon: UsersRound },
      { to: "/credits", label: "积分监控", icon: Coins },
      { to: "/credentials", label: "凭据", icon: BookKey },
    ],
  },
  {
    label: "代理与模型",
    items: [
      { to: "/proxy-keys", label: "代理密钥", icon: KeyRound },
      { to: "/models", label: "模型", icon: Boxes },
      { to: "/usage", label: "用量", icon: ChartNoAxesCombined },
    ],
  },
  {
    label: "自动化",
    items: [{ to: "/checkin", label: "签到", icon: CheckCircle2 }],
  },
  {
    label: "治理",
    items: [
      { to: "/settings", label: "设置", icon: Settings },
      { to: "/audit", label: "审计", icon: ClipboardList },
    ],
  },
];
const router = useRouter();
const session = useSessionStore();
const mobileNavigationOpen = ref(false);
const navigationCollapsed = ref(false);
const isMobileViewport = ref(false);
const service = useQuery({ queryKey: ["service"], queryFn: () => apiRequest<{ observed_state: string; in_flight: number }>("/service"), refetchInterval: 3000 });
const usage = useQuery({ queryKey: ["usage-summary"], queryFn: () => apiRequest<{ summary: { request_count: number } }>("/usage/summary"), refetchInterval: 10000 });
const metrics = useQuery({ queryKey: ["metrics"], queryFn: () => apiRequest<{ snapshots: { metric_kind: string; value: unknown }[] }>("/metrics/accounts"), refetchInterval: 30000 });
async function logout(): Promise<void> { await apiRequest("/session/logout", { method: "POST" }); session.clear(); await router.replace("/login"); }
function closeMobileNavigation(): void { mobileNavigationOpen.value = false; }
function updateViewport(): void {
  isMobileViewport.value = typeof window.matchMedia === "function"
    ? window.matchMedia("(max-width: 680px)").matches
    : window.innerWidth <= 680;
}

onMounted(() => {
  updateViewport();
  window.addEventListener("resize", updateViewport);
});

onBeforeUnmount(() => window.removeEventListener("resize", updateViewport));
</script>

<template>
  <div class="admin-shell" :class="{ 'admin-shell--nav-collapsed': navigationCollapsed }">
    <button
      v-if="mobileNavigationOpen"
      class="mobile-nav-backdrop"
      type="button"
      aria-label="关闭导航"
      @click="closeMobileNavigation"
    />
    <aside
      class="sidebar"
      :class="{
        'sidebar--collapsed': navigationCollapsed,
        'sidebar--mobile-open': mobileNavigationOpen,
      }"
      aria-label="主导航"
    >
      <div class="brand-lockup">
        <span class="brand-mark" aria-hidden="true">2</span>
        <span class="brand-lockup__copy">
          <strong>2api</strong>
          <small>多账号代理控制台</small>
        </span>
        <button
          v-if="isMobileViewport"
          class="icon-button mobile-nav-toggle"
          type="button"
          :aria-expanded="mobileNavigationOpen"
          aria-controls="control-navigation"
          @click="mobileNavigationOpen = !mobileNavigationOpen"
        >
          <Menu v-if="!mobileNavigationOpen" :size="19" aria-hidden="true" />
          <X v-else :size="19" aria-hidden="true" />
          <span class="sr-only">{{ mobileNavigationOpen ? "收起导航" : "展开导航" }}</span>
        </button>
        <button
          v-if="!isMobileViewport"
          class="icon-button desktop-nav-toggle"
          type="button"
          :aria-label="navigationCollapsed ? '展开导航' : '收起导航'"
          :title="navigationCollapsed ? '展开导航' : '收起导航'"
          @click="navigationCollapsed = !navigationCollapsed"
        >
          <ChevronRight v-if="navigationCollapsed" :size="17" aria-hidden="true" />
          <ChevronLeft v-else :size="17" aria-hidden="true" />
        </button>
      </div>

      <div class="sidebar-content" :class="{ 'sidebar-content--open': mobileNavigationOpen }">
        <nav id="control-navigation" class="nav-list">
          <section v-for="group in navigationGroups" :key="group.label" class="nav-group">
            <h2 class="nav-group__title">{{ group.label }}</h2>
            <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" :title="navigationCollapsed ? item.label : undefined" @click="closeMobileNavigation">
              <component :is="item.icon" :size="17" aria-hidden="true" />
              <span>{{ item.label }}</span>
            </RouterLink>
          </section>
        </nav>

        <div class="sidebar-footer">
          <DatabaseBackup :size="16" aria-hidden="true" />
          <span>SQLite · 本地加密</span>
        </div>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div class="topbar-context">
          <button
            v-if="isMobileViewport"
            class="icon-button topbar-menu-toggle"
            type="button"
            :aria-expanded="mobileNavigationOpen"
            aria-controls="control-navigation"
            @click="mobileNavigationOpen = !mobileNavigationOpen"
          >
            <Menu v-if="!mobileNavigationOpen" :size="19" aria-hidden="true" />
            <X v-else :size="19" aria-hidden="true" />
            <span class="sr-only">{{ mobileNavigationOpen ? "收起导航" : "展开导航" }}</span>
          </button>
          <div class="worker-state" role="status">
            <span class="status-dot" :class="`status-dot--${(service.data.value?.observed_state ?? 'unknown').toLowerCase()}`" aria-hidden="true"></span>
            <span>代理运行</span>
            <strong>{{ statusLabel(service.data.value?.observed_state) }}</strong>
          </div>
        </div>
        <div class="topbar-metrics" aria-label="运行摘要">
          <span><Activity :size="15" /> 活动请求 <strong>{{ service.data.value?.in_flight ?? 0 }}</strong></span>
          <span><Coins :size="15" /> 指标快照 <strong>{{ metrics.data.value?.snapshots?.length ?? 0 }}</strong></span>
          <span>今日请求 <strong>{{ usage.data.value?.summary?.request_count ?? 0 }}</strong></span>
          <button class="icon-button topbar-logout" type="button" title="退出登录" @click="logout"><LogOut :size="15" /></button>
        </div>
      </header>

      <main class="page-stage">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style src="../styles/admin-shell.css"></style>
