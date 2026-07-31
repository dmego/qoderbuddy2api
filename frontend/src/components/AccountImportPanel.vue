<script setup lang="ts">
import { CheckCircle2, ChevronDown, KeyRound, Link, LoaderCircle, LogIn, RefreshCcw } from "@lucide/vue";
import { computed, onBeforeUnmount, reactive, ref } from "vue";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";

export type AccountReference = { provider: string; account_id: string; label: string };

type Flow = { flow_id: string; auth_url: string; expires_at: string; label: string; account_id?: string | null };
type ImportResult = { account?: AccountReference; checkin_derived?: boolean; checkin_verified?: boolean };
type Provider = "codebuddy" | "qoder";

const props = withDefaults(defineProps<{
  provider?: Provider;
  accountId?: string;
  label?: string;
}>(), { provider: "codebuddy", accountId: "", label: "" });
const emit = defineEmits<{ saved: [account: AccountReference] }>();
const flowKey = "qb2api.codebuddy.oauth.flow";
const provider = ref<Provider>(props.provider);
const pending = ref(false);
const polling = ref(false);
const message = ref("");
const failed = ref(false);
const confirmCheckinVerification = ref(false);
const manualCheckinOpen = ref(false);
const flow = ref<Flow | null>(loadFlow());
let pollTimer: number | undefined;
const form = reactive({
  label: props.label,
  accountId: props.accountId,
  token: "",
  refreshToken: "",
  cookie: "",
  checkinMode: "bearer",
});

const requiresAccountId = computed(() => Boolean(props.accountId));
const chatTokenLabel = computed(() => provider.value === "qoder" ? "Personal Access Token (PAT)" : "Bearer Token");
const canSubmitChat = computed(() => form.token.trim().length > 0 && (!requiresAccountId.value || form.accountId.trim().length > 0));
const canSubmitCheckin = computed(() => {
  if (!form.accountId.trim()) return false;
  if (provider.value === "qoder") return form.token.trim().length > 0 && form.refreshToken.trim().length > 0;
  if (form.checkinMode === "cookie") return form.cookie.trim().length > 0;
  if (form.checkinMode === "bearer") return form.token.trim().length > 0;
  return form.token.trim().length > 0 || form.cookie.trim().length > 0;
});

function selectProvider(value: Provider): void {
  provider.value = value;
  manualCheckinOpen.value = false;
}

async function submitChat(): Promise<void> {
  if (!canSubmitChat.value) return;
  pending.value = true;
  setMessage("");
  try {
    const body: Record<string, string | undefined> = { label: form.label || undefined, account_id: form.accountId || undefined };
    if (provider.value === "qoder") body.pat = form.token;
    else body.token = form.token;
    const result = await apiRequest<ImportResult>(chatEndpoint(), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (result.account) form.accountId = result.account.account_id;
    clearSecrets();
    if (result.checkin_derived || result.checkin_verified) {
      setMessage("账号已保存，代理与签到均已自动启用。", false);
    } else {
      manualCheckinOpen.value = true;
      setMessage("代理凭据已保存。签到未能自动启用，可在下方手动导入。", false);
    }
    if (result.account) emit("saved", result.account);
  } catch (error) {
    setMessage(String(error), true);
  } finally {
    pending.value = false;
  }
}

async function submitCheckin(): Promise<void> {
  if (!canSubmitCheckin.value) return;
  if (provider.value === "codebuddy") {
    confirmCheckinVerification.value = true;
    return;
  }
  await doSubmitCheckin();
}

async function doSubmitCheckin(): Promise<void> {
  pending.value = true;
  setMessage("");
  try {
    const body: Record<string, string | undefined> = { account_id: form.accountId };
    if (provider.value === "qoder") {
      Object.assign(body, { access_token: form.token, refresh_token: form.refreshToken });
    } else {
      Object.assign(body, { mode: form.checkinMode, access_token: form.token || undefined, cookie: form.cookie || undefined });
    }
    const result = await apiRequest<ImportResult>(checkinEndpoint(), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    clearSecrets();
    confirmCheckinVerification.value = false;
    setMessage("签到凭据已验证并保存。", false);
    if (result.account) emit("saved", result.account);
  } catch (error) {
    setMessage(String(error), true);
  } finally {
    pending.value = false;
  }
}

function chatEndpoint(): string {
  return provider.value === "qoder" ? "/auth/qoder/chat" : "/auth/codebuddy/manual";
}

function checkinEndpoint(): string {
  return provider.value === "qoder" ? "/auth/qoder/checkin" : "/auth/codebuddy/checkin";
}

async function startOAuth(): Promise<void> {
  pending.value = true;
  setMessage("正在创建浏览器登录流程…", false);
  try {
    const started = await apiRequest<Flow>("/auth/codebuddy/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: form.label || "CodeBuddy OAuth", account_id: form.accountId || undefined }),
    });
    flow.value = started;
    saveFlow(started);
    window.open(started.auth_url, "_blank", "noopener,noreferrer");
    setMessage("已打开授权页；此页面会继续检查授权状态。", false);
    schedulePoll();
  } catch (error) {
    setMessage(String(error), true);
  } finally {
    pending.value = false;
  }
}

async function pollOAuth(): Promise<void> {
  if (!flow.value || polling.value) return;
  if (new Date(flow.value.expires_at).valueOf() <= Date.now()) {
    clearFlow();
    setMessage("OAuth 登录流程已过期，请重新开始。", true);
    return;
  }
  polling.value = true;
  try {
    const result = await apiRequest<ImportResult & { status: string; message?: string }>("/auth/codebuddy/poll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flow_id: flow.value.flow_id }),
    });
    if (result.status === "pending") {
      setMessage("等待在浏览器中完成授权…", false);
      return;
    }
    clearFlow();
    if (result.status !== "success" || !result.account) {
      setMessage(result.message || "授权未完成，可重试或重新开始。", true);
      return;
    }
    clearSecrets();
    form.accountId = result.account.account_id;
    if (result.checkin_verified) {
      setMessage("OAuth 登录成功，代理与签到均已自动启用。", false);
    } else {
      manualCheckinOpen.value = true;
      setMessage("OAuth 登录成功，代理已启用。签到未能自动验证，可在下方手动导入。", false);
    }
    emit("saved", result.account);
  } catch (error) {
    setMessage(String(error), true);
  } finally {
    polling.value = false;
  }
}

function schedulePoll(): void {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(async () => {
    await pollOAuth();
    if (flow.value) schedulePoll();
  }, 2_000);
}

function setMessage(value: string, isFailure = false): void { message.value = value; failed.value = isFailure; }
function clearSecrets(): void { Object.assign(form, { token: "", refreshToken: "", cookie: "" }); }
function saveFlow(value: Flow): void { sessionStorage.setItem(flowKey, JSON.stringify(value)); }
function clearFlow(): void { window.clearTimeout(pollTimer); flow.value = null; sessionStorage.removeItem(flowKey); }
function loadFlow(): Flow | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(flowKey) || "null") as Flow | null;
    return value && new Date(value.expires_at).valueOf() > Date.now() ? value : null;
  } catch { return null; }
}

onBeforeUnmount(() => window.clearTimeout(pollTimer));
</script>

<template>
  <section class="import-panel" aria-label="账号导入">
    <div class="segmented-control" aria-label="服务提供方"><button type="button" :class="{ active: provider === 'codebuddy' }" @click="selectProvider('codebuddy')">CodeBuddy</button><button type="button" :class="{ active: provider === 'qoder' }" @click="selectProvider('qoder')">Qoder</button></div>
    <div class="form-grid">
      <label>显示名称<input v-model="form.label" aria-label="显示名称" autocomplete="off" placeholder="例如：主账号" /></label>
      <label v-if="requiresAccountId">已有账号 ID<span class="required-mark">必填</span><input v-model="form.accountId" aria-label="账号 ID" autocomplete="off" /></label>
    </div>

    <!-- 主入口：CodeBuddy 浏览器登录 -->
    <div v-if="provider === 'codebuddy'" class="form-actions">
      <button type="button" :disabled="pending" @click="startOAuth"><LogIn :size="16" />浏览器登录</button>
      <button v-if="flow" class="secondary-button" type="button" :disabled="polling" @click="pollOAuth"><RefreshCcw :class="{ spin: polling }" :size="16" />继续 OAuth 登录</button>
    </div>
    <p v-if="flow" class="helper-text">流程将在 {{ new Date(flow.expires_at).toLocaleTimeString() }} 过期；可离开此页后返回继续轮询。</p>

    <!-- Qoder: PAT 输入 -->
    <div v-if="provider === 'qoder'" class="form-grid">
      <label class="form-span">{{ chatTokenLabel }}<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.token" :aria-label="chatTokenLabel" type="password" autocomplete="new-password" /></div></label>
      <p class="helper-text form-span">在 QoderWork 客户端「设置 -> 个人令牌」中生成 PAT，粘贴到此处的密码框。签到凭据会自动从 PAT 派生。</p>
      <div class="form-actions form-span">
        <button type="button" :disabled="pending || !canSubmitChat" @click="submitChat"><LoaderCircle v-if="pending" class="spin" :size="16" /><Link v-else :size="16" />验证并保存</button>
      </div>
    </div>

    <!-- CodeBuddy 手动 Bearer Token（折叠） -->
    <details v-if="provider === 'codebuddy'" class="advanced-section">
      <summary><ChevronDown :size="14" /> 手动输入 Bearer Token</summary>
      <div class="form-grid">
        <label class="form-span">{{ chatTokenLabel }}<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.token" aria-label="Bearer Token" type="password" autocomplete="new-password" /></div></label>
      </div>
      <div class="form-actions">
        <button type="button" :disabled="pending || !canSubmitChat" @click="submitChat"><LoaderCircle v-if="pending" class="spin" :size="16" /><Link v-else :size="16" />验证并保存</button>
      </div>
    </details>

    <!-- 手动导入签到凭据（折叠，自动派生失败时使用） -->
    <details class="advanced-section" :open="manualCheckinOpen">
      <summary><ChevronDown :size="14" /> 手动导入签到凭据</summary>
      <div class="form-grid">
        <label v-if="!requiresAccountId" class="form-span">账号 ID<span class="required-mark">必填</span><input v-model="form.accountId" aria-label="账号 ID" autocomplete="off" /></label>
        <label v-if="provider === 'codebuddy'">认证模式<select v-model="form.checkinMode" aria-label="签到认证模式"><option value="bearer">Bearer Token</option><option value="cookie">Cookie</option><option value="bearer_cookie">Bearer Token + Cookie</option></select></label>
        <label v-if="provider === 'codebuddy' && form.checkinMode !== 'cookie'" class="form-span">WorkBuddy Bearer Token<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.token" aria-label="WorkBuddy Bearer Token" type="password" autocomplete="new-password" /></div></label>
        <label v-if="provider === 'codebuddy' && form.checkinMode !== 'bearer'" class="form-span">WorkBuddy Cookie<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.cookie" aria-label="WorkBuddy Cookie" type="password" autocomplete="new-password" /></div></label>
        <label v-if="provider === 'qoder'" class="form-span">Qoder 签到 Access Token<span class="required-mark">必填</span><input v-model="form.token" aria-label="Qoder Access Token" type="password" autocomplete="new-password" /></label>
        <label v-if="provider === 'qoder'" class="form-span">Qoder 刷新令牌<span class="required-mark">必填</span><input v-model="form.refreshToken" aria-label="Qoder 刷新令牌" type="password" autocomplete="new-password" /></label>
      </div>
      <p v-if="provider === 'qoder'" class="helper-text">签到 token 通常可通过 PAT 自动派生（推荐）；此入口仅在自动派生失败时使用。</p>
      <div class="form-actions">
        <button type="button" :disabled="pending || !canSubmitCheckin" @click="submitCheckin"><LoaderCircle v-if="pending" class="spin" :size="16" /><Link v-else :size="16" />验证并启用</button>
      </div>
    </details>

    <p class="helper-text">原始凭据只用于本次受保护请求；成功后表单立即清空，管理台只显示版本和状态。</p>
    <p v-if="message" class="form-message" :class="{ 'text-danger': failed }" :role="failed ? 'alert' : 'status'"><CheckCircle2 v-if="!failed" :size="15" />{{ message }}</p>
    <ConfirmDialog :open="confirmCheckinVerification" title="验证并启用 WorkBuddy 签到？" description="系统将发送一次 WorkBuddy 每日签到请求；未签到时可能立即领取当天积分。仅在成功或已签到后才会启用定时签到。" confirm-label="确认并验证" :busy="pending" @cancel="confirmCheckinVerification = false" @confirm="doSubmitCheckin" />
  </section>
</template>
