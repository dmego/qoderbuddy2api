<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import {
  Clipboard,
  KeyRound,
  Plus,
  RefreshCcw,
  RotateCw,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  X,
} from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";

import { apiRequest } from "@/api/client";
import PanelHeader from "@/components/PanelHeader.vue";
import StatePill from "@/components/StatePill.vue";

type ProxyKey = {
  key_id: string;
  name: string;
  scopes: string[];
  enabled: boolean;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
};

type RevealedKey = {
  key_id: string;
  key: string;
  name: string;
  expires_at: string | null;
  replaced_key_id?: string;
  runtime_apply?: { status: "succeeded" | "failed"; error_code?: string };
};

type PendingAction = { kind: "rotate" | "revoke"; key: ProxyKey };

const form = reactive({ name: "", expiresAt: "" });
const revealed = ref<RevealedKey | null>(null);
const confirmation = ref<PendingAction | null>(null);
const busy = ref("");
const error = ref("");
const copyState = ref("");

const keyQuery = useQuery({
  queryKey: ["proxy-keys"],
  queryFn: () => apiRequest<{ keys: ProxyKey[] }>("/proxy-keys"),
});

const rows = computed(() => keyQuery.data.value?.keys ?? []);
const activeCount = computed(() => rows.value.filter((item) => keyStatus(item) === "active").length);
const expiringCount = computed(() => rows.value.filter(isExpiringSoon).length);
const revokedCount = computed(() => rows.value.filter((item) => keyStatus(item) === "revoked").length);

async function createKey(): Promise<void> {
  busy.value = "create";
  error.value = "";
  try {
    const result = await apiRequest<RevealedKey>("/proxy-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: form.name.trim() || "Proxy key",
        scopes: ["proxy"],
        expires_at: form.expiresAt ? new Date(form.expiresAt).toISOString() : null,
      }),
    });
    revealKey(result);
    form.name = "";
    form.expiresAt = "";
    await keyQuery.refetch();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = "";
  }
}

async function confirmAction(): Promise<void> {
  const action = confirmation.value;
  if (!action) return;
  busy.value = `${action.kind}:${action.key.key_id}`;
  error.value = "";
  try {
    const result = await apiRequest<RevealedKey | { status: string }>(
      `/proxy-keys/${action.key.key_id}/${action.kind}`,
      { method: "POST" },
    );
    if (action.kind === "rotate") revealKey(result as RevealedKey);
    confirmation.value = null;
    await keyQuery.refetch();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = "";
  }
}

async function copySecret(): Promise<void> {
  if (!revealed.value) return;
  try {
    await navigator.clipboard.writeText(revealed.value.key);
    copyState.value = "已复制到剪贴板";
  } catch {
    copyState.value = "复制失败，请手动选择";
  }
}

function dismissSecret(): void {
  revealed.value = null;
  copyState.value = "";
}

function revealKey(result: RevealedKey): void {
  revealed.value = result;
  copyState.value = "";
  if (result.runtime_apply?.status === "failed") {
    error.value = "Key 已安全保存，但 Worker 热加载失败。请保留本次明文，并在服务页重试 reload。";
  }
}

function keyStatus(item: ProxyKey): "active" | "expired" | "revoked" {
  if (!item.enabled || item.revoked_at) return "revoked";
  if (item.expires_at && new Date(item.expires_at).getTime() <= Date.now()) return "expired";
  return "active";
}

function isExpiringSoon(item: ProxyKey): boolean {
  if (keyStatus(item) !== "active" || !item.expires_at) return false;
  const remaining = new Date(item.expires_at).getTime() - Date.now();
  return remaining <= 7 * 24 * 60 * 60 * 1000;
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "操作失败，请刷新后重试";
}
</script>

<template>
  <section class="page-content proxy-key-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">Access control</p>
        <h1>Proxy API Keys</h1>
        <p>为 Codex、Claude Code 和其他调用端分配独立密钥，并即时轮换或撤销。</p>
      </div>
      <button class="secondary-button" type="button" @click="keyQuery.refetch()">
        <RefreshCcw :size="16" />刷新状态
      </button>
    </header>

    <div class="security-banner">
      <ShieldCheck :size="19" aria-hidden="true" />
      <div>
        <strong>Proxy 与管理权限完全隔离</strong>
        <span>这里创建的 Key 只能访问模型兼容 API，不能登录管理台。数据库只保存不可逆 hash，明文仅在创建或轮换后展示一次。</span>
      </div>
    </div>

    <div class="summary-grid proxy-key-summary" aria-label="Proxy Key 摘要">
      <article class="summary-tile"><KeyRound :size="20" /><span>全部密钥</span><strong>{{ rows.length }}</strong><small>包含历史撤销记录</small></article>
      <article class="summary-tile"><ShieldCheck :size="20" /><span>当前有效</span><strong>{{ activeCount }}</strong><small>Worker 可立即验证</small></article>
      <article class="summary-tile"><ShieldAlert :size="20" /><span>7 天内过期</span><strong>{{ expiringCount }}</strong><small>建议提前轮换</small></article>
      <article class="summary-tile"><Trash2 :size="20" /><span>已撤销</span><strong>{{ revokedCount }}</strong><small>不可重新启用</small></article>
    </div>

    <section v-if="revealed" class="secret-reveal" aria-live="assertive">
      <div class="secret-reveal__heading">
        <div>
          <p class="eyebrow">One-time reveal</p>
          <h2>{{ revealed.replaced_key_id ? "轮换后的新 Key" : "新 Proxy Key" }}</h2>
          <p><strong>仅显示这一次。</strong>关闭后服务端无法恢复明文，请立即保存到调用端的安全配置。</p>
        </div>
        <button data-test="dismiss-secret" class="icon-button" type="button" title="关闭并清除明文" @click="dismissSecret"><X :size="17" /></button>
      </div>
      <div class="secret-value">
        <code>{{ revealed.key }}</code>
        <button data-test="copy-secret" type="button" @click="copySecret"><Clipboard :size="16" />复制</button>
      </div>
      <small>{{ copyState || `Key ID: ${revealed.key_id}` }}</small>
    </section>

    <div class="proxy-key-layout">
      <section class="data-panel create-key-panel">
        <PanelHeader title="创建调用密钥" description="建议为每个客户端或自动化任务使用独立 Key" />
        <form class="key-form" @submit.prevent="createKey">
          <label for="proxy-key-name">名称<input id="proxy-key-name" v-model="form.name" maxlength="80" placeholder="例如：Mac mini Codex" /></label>
          <label for="proxy-key-expiry">过期时间（可选）<input id="proxy-key-expiry" v-model="form.expiresAt" type="datetime-local" /></label>
          <div class="key-scope"><span>固定权限</span><strong>proxy</strong><small>不可访问 /api/admin/*</small></div>
          <button data-test="create-key" type="submit" :disabled="busy === 'create'">
            <Plus :size="16" />{{ busy === "create" ? "正在创建" : "创建并显示一次" }}
          </button>
        </form>
      </section>

      <section class="data-panel key-policy-panel">
        <PanelHeader title="运行时策略" description="密钥变更通过 snapshot 热加载到 Proxy Worker" />
        <dl class="detail-list">
          <div><dt>创建 / 轮换</dt><dd>响应完成后新 Key 即可用于模型请求</dd></div>
          <div><dt>撤销</dt><dd>Worker 重载后立即返回 401，无需重启服务</dd></div>
          <div><dt>服务端存储</dt><dd>带盐 hash；不保存可逆明文</dd></div>
          <div><dt>浏览器存储</dt><dd>不写 localStorage / sessionStorage</dd></div>
        </dl>
      </section>
    </div>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>

    <section class="data-panel">
      <PanelHeader title="密钥生命周期" description="历史记录可用于审计；撤销后的 Key 不可恢复">
        <span class="status-label">{{ rows.length }} 条记录</span>
      </PanelHeader>
      <div v-if="keyQuery.isPending.value" class="loading-row">正在读取 Proxy Key 元数据…</div>
      <div v-else-if="keyQuery.isError.value" class="alert" role="alert">读取失败：{{ keyQuery.error.value }}</div>
      <div v-else-if="rows.length === 0" class="empty-state"><KeyRound :size="25" /><strong>还没有动态 Proxy Key</strong><span>创建后可立即分配给调用端，旧环境变量 Key 可在迁移期继续使用。</span></div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>名称 / ID</th><th>状态</th><th>权限</th><th>创建时间</th><th>过期时间</th><th>最近使用</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="item in rows" :key="item.key_id">
              <td><strong>{{ item.name }}</strong><small class="mono" :title="item.key_id">{{ item.key_id }}</small></td>
              <td><StatePill :value="keyStatus(item)" /></td>
              <td><span class="provider-mark">{{ item.scopes.join(", ") }}</span></td>
              <td>{{ formatDate(item.created_at) }}</td>
              <td :class="{ 'text-warning': isExpiringSoon(item) }">{{ formatDate(item.expires_at) }}</td>
              <td>{{ formatDate(item.last_used_at) }}</td>
              <td>
                <div class="row-actions">
                  <button class="icon-button" type="button" title="轮换" :disabled="keyStatus(item) !== 'active' || !!busy" @click="confirmation = { kind: 'rotate', key: item }"><RotateCw :size="15" /></button>
                  <button :data-test="`revoke-${item.key_id}`" class="icon-button danger-icon" type="button" title="撤销" :disabled="keyStatus(item) !== 'active' || !!busy" @click="confirmation = { kind: 'revoke', key: item }"><Trash2 :size="15" /></button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="confirmation" class="confirm-backdrop" @click.self="confirmation = null">
      <section class="confirm-card" role="alertdialog" aria-modal="true" aria-labelledby="proxy-key-confirm-title">
        <ShieldAlert :size="24" aria-hidden="true" />
        <div>
          <p class="eyebrow">Destructive action</p>
          <h2 id="proxy-key-confirm-title">{{ confirmation.kind === "revoke" ? "确认撤销 Proxy Key" : "确认轮换 Proxy Key" }}</h2>
          <p>
            <strong>{{ confirmation.key.name }}</strong> 当前正在使用的明文将立即失效。
            {{ confirmation.kind === "rotate" ? "新 Key 只会显示一次。" : "该操作无法撤销。" }}
          </p>
        </div>
        <div class="confirm-actions">
          <button class="secondary-button" type="button" @click="confirmation = null">取消</button>
          <button data-test="confirm-destructive" class="danger-button" type="button" :disabled="!!busy" @click="confirmAction">
            {{ confirmation.kind === "revoke" ? "撤销并使其失效" : "轮换并显示新 Key" }}
          </button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped src="../styles/proxy-keys.css"></style>
