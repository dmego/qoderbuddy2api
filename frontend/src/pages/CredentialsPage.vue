<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { KeyRound, RefreshCcw, RotateCw, ShieldOff } from "@lucide/vue";
import { reactive, ref } from "vue";

import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";
import { apiRequest } from "@/api/client";

type Credential = { provider: string; account_id: string; purpose: string; mode: string; payload_version: number; credential_version: number; expires_at?: string; has_refresh_token: boolean; updated_at: string };
const queryClient = useQueryClient();
const filter = ref("all");
const selected = ref<Credential | null>(null);
const form = reactive({ token: "", refreshToken: "" });
const credentials = useQuery({ queryKey: ["credentials"], queryFn: () => apiRequest<{ credentials: Credential[] }>("/credentials") });
const mutation = useMutation({
  mutationFn: async (kind: "rotate" | "revoke") => {
    if (!selected.value) throw new Error("请选择凭据");
    const base = `/credentials/${selected.value.provider}/${selected.value.account_id}/${selected.value.purpose}`;
    if (kind === "revoke") return apiRequest(base, { method: "DELETE" });
    return apiRequest(base, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: form.token, refresh_token: form.refreshToken || undefined }) });
  },
  onSuccess: async () => { form.token = ""; form.refreshToken = ""; await queryClient.invalidateQueries({ queryKey: ["credentials"] }); },
});
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><p class="eyebrow">Secret metadata</p><h1>凭据管理</h1><p>只显示模式、版本、过期时间和刷新能力；密文永远不会回到浏览器。</p></div><button class="secondary-button" type="button" @click="credentials.refetch()"><RefreshCcw :size="16" />刷新元数据</button></header>
    <div class="security-banner"><KeyRound :size="18" /><div><strong>凭据隔离</strong><span>Proxy、check-in 和 chat 按 purpose 分开；旋转会增加 credential version，不会改变账号 ID。</span></div></div>
    <section class="data-panel"><PanelHeader title="已保存凭据" description="删除凭据后对应用途会进入 needs_reauth"><template #default><select v-model="filter" aria-label="筛选凭据"><option value="all">全部</option><option value="chat">chat</option><option value="checkin">checkin</option></select></template></PanelHeader><div v-if="credentials.isPending.value" class="loading-row">正在读取凭据元数据…</div><div v-else class="table-wrap"><table><thead><tr><th>账号</th><th>用途</th><th>模式</th><th>版本</th><th>过期时间</th><th>刷新能力</th><th></th></tr></thead><tbody><tr v-for="item in (credentials.data.value?.credentials ?? []).filter((row) => filter === 'all' || row.purpose === filter)" :key="`${item.provider}:${item.account_id}:${item.purpose}`"><td><strong>{{ item.provider }}</strong><small>{{ item.account_id }}</small></td><td><StatePill :value="item.purpose" /></td><td>{{ item.mode }}</td><td class="mono">v{{ item.credential_version }} / p{{ item.payload_version }}</td><td>{{ item.expires_at ?? "不设过期" }}</td><td>{{ item.has_refresh_token ? "可刷新" : "无 refresh" }}</td><td><button class="icon-button" type="button" title="编辑凭据" @click="selected = item"><RotateCw :size="16" /></button></td></tr></tbody></table></div></section>
    <section v-if="selected" class="data-panel credential-editor"><PanelHeader title="轮换凭据" :description="`${selected.provider} / ${selected.account_id} / ${selected.purpose}`" /><div class="form-grid"><label>新 Token<input v-model="form.token" type="password" autocomplete="new-password" /></label><label>新 Refresh Token（可选）<input v-model="form.refreshToken" type="password" autocomplete="new-password" /></label></div><div class="form-actions"><button type="button" :disabled="!form.token || mutation.isPending.value" @click="mutation.mutate('rotate')"><RotateCw :size="16" />验证并轮换</button><button class="danger-button" type="button" :disabled="mutation.isPending.value" @click="mutation.mutate('revoke')"><ShieldOff :size="16" />撤销用途</button></div><p v-if="mutation.isError.value" class="form-error">{{ mutation.error.value }}</p></section>
  </section>
</template>
