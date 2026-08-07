<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { Eye, EyeOff, KeyRound, RefreshCcw, RotateCw, Search, ShieldOff, X } from "@lucide/vue";
import { computed, reactive, ref, watch } from "vue";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import NotificationRegion from "@/components/NotificationRegion.vue";
import OperationStatus from "@/components/OperationStatus.vue";
import PaginatedTable from "@/components/PaginatedTable.vue";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { appendQuery } from "@/composables/useCursorPager";
import { useNotifications } from "@/composables/useNotifications";

type Credential = { provider: string; account_id: string; purpose: string; mode: string; payload_version: number; credential_version: number; expires_at?: string; has_refresh_token: boolean; updated_at: string; source?: string };

const queryClient = useQueryClient();
const provider = ref("");
const purpose = ref("");
const expiry = ref("");
const search = ref("");
const selected = ref<Credential | null>(null);
const pending = ref<"rotate" | "revoke" | null>(null);
const showSecrets = ref(false);
const page = ref(1);
const pageSize = 15;
const form = reactive({ token: "", cookie: "", mode: "bearer", refreshToken: "", expiresAt: "" });
const lastOperation = ref<Record<string, unknown> | null>(null);
const { notifications, notify, dismiss } = useNotifications();

const credentials = useQuery({ queryKey: ["credentials", provider], queryFn: () => apiRequest<{ credentials: Credential[] }>(appendQuery("/credentials", { provider: provider.value })), staleTime: 60_000 });
const filtered = computed(() => (credentials.data.value?.credentials ?? []).filter((item) => {
  const matchesSearch = `${item.provider} ${item.account_id}`.toLowerCase().includes(search.value.trim().toLowerCase());
  return matchesSearch && (!purpose.value || item.purpose === purpose.value) && (!expiry.value || expiryState(item) === expiry.value);
}));
const visible = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize));
watch([provider, purpose, expiry, search], () => { page.value = 1; });

const mutation = useMutation({
  mutationFn: async (kind: "rotate" | "revoke") => {
    if (!selected.value) throw new Error("请选择凭据");
    const base = `/credentials/${encodeURIComponent(selected.value.provider)}/${encodeURIComponent(selected.value.account_id)}/${encodeURIComponent(selected.value.purpose)}`;
    if (kind === "revoke") return apiRequest<Record<string, unknown>>(`${base}/revoke`, { method: "POST" });
    return apiRequest<Record<string, unknown>>(`${base}/rotate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: form.token || undefined, cookie: form.cookie || undefined, mode: form.mode, refresh_token: form.refreshToken || undefined, expires_at: form.expiresAt || undefined, credential_version: selected.value.credential_version }) });
  },
  onSuccess: async (result, kind) => {
    lastOperation.value = { action: kind === "rotate" ? "轮换凭据" : "撤销凭据", status: "succeeded", account_id: selected.value?.account_id, ...result };
    notify(kind === "rotate" ? "凭据已轮换" : "凭据已撤销", { message: selected.value ? `${selected.value.provider} / ${selected.value.account_id} / ${selected.value.purpose}` : undefined, tone: kind === "rotate" ? "success" : "warning" });
    Object.assign(form, { token: "", cookie: "", mode: "bearer", refreshToken: "", expiresAt: "" }); selected.value = null;
    await queryClient.invalidateQueries({ queryKey: ["credentials"] });
  },
  onError: (error) => notify("凭据操作失败", { message: String(error), tone: "error", timeout: 0 }),
});

const needsToken = computed(() => form.mode !== "cookie");
const needsCookie = computed(() => form.mode === "cookie" || form.mode === "bearer_cookie");
const canRotate = computed(() => (!needsToken.value || Boolean(form.token)) && (!needsCookie.value || Boolean(form.cookie)));
const modeChoices = computed(() => {
  if (selected.value?.provider === "qoder") return selected.value.purpose === "chat" ? ["pat"] : ["access_refresh"];
  return ["bearer", "oauth", "cookie", "bearer_cookie"];
});
function select(item: Credential): void { selected.value = item; Object.assign(form, { token: "", cookie: "", mode: item.mode, refreshToken: "", expiresAt: item.expires_at?.slice(0, 16) ?? "" }); }
function expiryState(item: Credential): string { if (!item.expires_at) return "no_expiry"; const remaining = new Date(item.expires_at).valueOf() - Date.now(); if (remaining <= 0) return "expired"; return remaining < 7 * 86_400_000 ? "expiring" : "valid"; }
function clearFilters(): void { provider.value = ""; purpose.value = ""; expiry.value = ""; search.value = ""; }
function confirmMutation(): void { if (pending.value) mutation.mutate(pending.value); pending.value = null; }
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>凭据管理</h1><p>只展示版本、模式、过期状态与刷新能力；密文和凭据指纹永不返回浏览器。</p></div><button class="secondary-button" type="button" :disabled="credentials.isFetching.value" @click="credentials.refetch()"><RefreshCcw :class="{ spin: credentials.isFetching.value }" :size="16" />刷新元数据</button></header>
    <div class="security-banner"><KeyRound :size="18" /><div><strong>用途隔离与并发保护</strong><span>代理请求与每日签到独立轮换；提交会携带凭据版本，版本冲突时不会覆盖较新的凭据。</span></div></div>

    <section class="data-panel filter-panel"><PanelHeader title="凭据筛选" description="筛选仅作用于安全元数据。"><Search :size="17" /></PanelHeader><div class="filter-grid filter-grid--five"><label class="filter-search">账号<div class="input-with-icon"><Search :size="15" /><input v-model="search" placeholder="服务提供方或账号 ID" /></div></label><label>服务提供方<select v-model="provider"><option value="">全部</option><option value="codebuddy">CodeBuddy</option><option value="qoder">Qoder</option></select></label><label>用途<select v-model="purpose"><option value="">全部</option><option value="chat">代理请求</option><option value="checkin">每日签到</option></select></label><label>到期状态<select v-model="expiry"><option value="">全部</option><option value="expiring">7 天内到期</option><option value="expired">已到期</option><option value="valid">有效</option><option value="no_expiry">无到期时间</option></select></label><div class="filter-actions"><button class="secondary-button" type="button" @click="clearFilters"><X :size="15" />清除</button></div></div></section>

    <section class="data-panel"><PanelHeader title="已保存凭据" :description="`${filtered.length} 条安全元数据 · 第 ${page} 页`" /><PaginatedTable aria-label="凭据元数据" :loading="credentials.isPending.value" :error="credentials.isError.value ? `凭据读取失败：${credentials.error.value}` : ''" :empty="!visible.length" empty-title="没有匹配的凭据" empty-description="调整筛选条件，或先在账号页导入用途凭据。" :stale="credentials.isStale.value" :page="page" :page-size="pageSize" :total="filtered.length" :can-previous="page > 1" :can-next="page * pageSize < filtered.length" @retry="credentials.refetch()" @previous="page -= 1" @next="page += 1"><template #header><tr><th>账号</th><th>用途</th><th>模式</th><th>版本</th><th>过期状态</th><th>刷新能力</th><th>更新时间</th><th>操作</th></tr></template><tr v-for="item in visible" :key="`${item.provider}:${item.account_id}:${item.purpose}`" :class="{ selected: selected === item }"><td><strong>{{ item.provider }}</strong><small>{{ item.account_id }}</small></td><td><StatePill :value="item.purpose" /></td><td>{{ item.mode }}</td><td class="mono">v{{ item.credential_version }} / p{{ item.payload_version }}</td><td><StatePill :value="expiryState(item)" /><small>{{ item.expires_at ?? "不设过期" }}</small></td><td>{{ item.has_refresh_token ? "可刷新" : "无 refresh" }}</td><td>{{ item.updated_at }}</td><td><button class="icon-button" type="button" :aria-label="`编辑 ${item.account_id} ${item.purpose} 凭据`" :title="`编辑 ${item.account_id} ${item.purpose} 凭据`" @click="select(item)"><RotateCw :size="16" /></button></td></tr></PaginatedTable></section>

    <section v-if="selected" class="data-panel credential-editor"><PanelHeader title="轮换凭据" :description="`${selected.provider} / ${selected.account_id} / ${selected.purpose}`"><button class="icon-button" type="button" aria-label="关闭凭据编辑器" title="关闭凭据编辑器" @click="selected = null"><X :size="15" /></button></PanelHeader><div class="form-grid"><label>凭据模式<select v-model="form.mode" aria-label="凭据模式"><option v-for="option in modeChoices" :key="option" :value="option">{{ option }}</option></select></label><label v-if="needsToken">新 Token <span class="required-mark">必填</span><div class="input-with-icon"><KeyRound :size="15" /><input v-model="form.token" aria-label="新 Token" :type="showSecrets ? 'text' : 'password'" autocomplete="new-password" /></div></label><label v-if="needsCookie">新 Cookie <span class="required-mark">必填</span><div class="input-with-icon"><KeyRound :size="15" /><input v-model="form.cookie" aria-label="新 Cookie" :type="showSecrets ? 'text' : 'password'" autocomplete="new-password" /></div></label><label>新刷新令牌（可选）<input v-model="form.refreshToken" :type="showSecrets ? 'text' : 'password'" autocomplete="new-password" /></label><label>到期时间（可选）<input v-model="form.expiresAt" type="datetime-local" /></label><label class="inline-check"><input v-model="showSecrets" type="checkbox" /><Eye v-if="showSecrets" :size="15" /><EyeOff v-else :size="15" />暂时显示输入内容</label></div><p class="helper-text">服务端先校验字段、版本与用途契约；成功后旧密文原子替换，响应不回显凭据。</p><div class="form-actions"><button type="button" :disabled="!canRotate || mutation.isPending.value" @click="pending = 'rotate'"><RotateCw :size="16" />验证版本并轮换</button><button class="danger-button" type="button" :disabled="mutation.isPending.value" @click="pending = 'revoke'"><ShieldOff :size="16" />撤销用途</button></div></section>

    <OperationStatus :operation="lastOperation" />
    <ConfirmDialog :open="Boolean(pending)" :title="pending === 'revoke' ? '撤销这项凭据？' : '轮换这项凭据？'" :description="pending === 'revoke' ? '撤销后该用途会立即停止参与调度，并进入 needs_reauth。' : '轮换会使旧凭据失效，并要求重新验证用途状态。'" :confirm-label="pending === 'revoke' ? '确认撤销' : '确认轮换'" :tone="pending === 'revoke' ? 'danger' : 'default'" :verification-text="pending === 'revoke' ? 'REVOKE' : ''" :busy="mutation.isPending.value" @cancel="pending = null" @confirm="confirmMutation" />
    <NotificationRegion :notifications="notifications" @dismiss="dismiss" />
  </section>
</template>
