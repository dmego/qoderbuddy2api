<script setup lang="ts">
import {
  Activity,
  BookKey,
  Boxes,
  ChartNoAxesCombined,
  CheckCircle2,
  CircleGauge,
  ClipboardList,
  Coins,
  DatabaseBackup,
  KeyRound,
  Power,
  Settings,
  UsersRound,
  LogOut,
} from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { RouterLink, RouterView } from "vue-router";
import { useRouter } from "vue-router";

import { apiRequest } from "@/api/client";
import { useSessionStore } from "@/stores/session";

const navigation = [
  { to: "/overview", label: "总览", icon: CircleGauge },
  { to: "/service", label: "代理服务", icon: Power },
  { to: "/accounts", label: "账号", icon: UsersRound },
  { to: "/credentials", label: "凭据", icon: BookKey },
  { to: "/proxy-keys", label: "Proxy Keys", icon: KeyRound },
  { to: "/models", label: "模型", icon: Boxes },
  { to: "/usage", label: "用量", icon: ChartNoAxesCombined },
  { to: "/checkin", label: "签到", icon: CheckCircle2 },
  { to: "/settings", label: "设置", icon: Settings },
  { to: "/audit", label: "审计", icon: ClipboardList },
];
const router = useRouter();
const session = useSessionStore();
const service = useQuery({ queryKey: ["service"], queryFn: () => apiRequest<{ observed_state: string; in_flight: number }>("/service"), refetchInterval: 3000 });
const usage = useQuery({ queryKey: ["usage-summary"], queryFn: () => apiRequest<{ summary: { request_count: number } }>("/usage/summary"), refetchInterval: 10000 });
const metrics = useQuery({ queryKey: ["metrics"], queryFn: () => apiRequest<{ snapshots: { metric_kind: string; value: unknown }[] }>("/metrics/accounts"), refetchInterval: 30000 });
async function logout(): Promise<void> { await apiRequest("/session/logout", { method: "POST" }); session.clear(); await router.replace("/login"); }
</script>

<template>
  <div class="admin-shell">
    <aside class="sidebar" aria-label="主导航">
      <div class="brand-lockup">
        <span class="brand-mark" aria-hidden="true">2</span>
        <span>
          <strong>2api</strong>
          <small>Control Plane</small>
        </span>
      </div>

      <nav class="nav-list">
        <RouterLink v-for="item in navigation" :key="item.to" :to="item.to">
          <component :is="item.icon" :size="17" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <DatabaseBackup :size="16" aria-hidden="true" />
        <span>SQLite · 本地加密</span>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div class="worker-state" role="status">
          <span class="status-dot" :class="`status-dot--${(service.data.value?.observed_state ?? 'unknown').toLowerCase()}`" aria-hidden="true"></span>
          <span>Proxy Worker</span>
          <strong>{{ service.data.value?.observed_state ?? "加载中" }}</strong>
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
