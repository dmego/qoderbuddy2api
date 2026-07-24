<script setup lang="ts">
import { CheckCircle2, KeyRound, Link, LoaderCircle, LogIn, RefreshCcw } from "@lucide/vue";
import { computed, onBeforeUnmount, reactive, ref } from "vue";

import { apiRequest } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";

export type AccountReference = { provider: string; account_id: string; label: string };

type Flow = { flow_id: string; auth_url: string; expires_at: string; label: string; account_id?: string | null };
type ImportResult = { account?: AccountReference };
type Provider = "codebuddy" | "qoder";
type Purpose = "chat" | "checkin";

const props = withDefaults(defineProps<{
  provider?: Provider;
  purpose?: Purpose;
  accountId?: string;
  label?: string;
}>(), { provider: "codebuddy", purpose: undefined, accountId: "", label: "" });
const emit = defineEmits<{ saved: [account: AccountReference] }>();
const flowKey = "qb2api.codebuddy.oauth.flow";
const provider = ref<Provider>(props.provider);
const purpose = ref<Purpose>(props.purpose || (props.accountId ? "checkin" : "chat"));
const pending = ref(false);
const polling = ref(false);
const message = ref("");
const failed = ref(false);
const confirmCheckinVerification = ref(false);
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

const requiresAccountId = computed(() => purpose.value === "checkin" || Boolean(props.accountId));
const tokenLabel = computed(() => {
  if (provider.value === "qoder") return purpose.value === "chat" ? "Personal access token" : "Access token";
  return purpose.value === "chat" ? "Bearer token" : "WorkBuddy Bearer token";
});
const showToken = computed(() => !(provider.value === "codebuddy" && purpose.value === "checkin" && form.checkinMode === "cookie"));
const showCookie = computed(() => provider.value === "codebuddy" && purpose.value === "checkin" && form.checkinMode !== "bearer");
const canSubmit = computed(() => {
  if (requiresAccountId.value && !form.accountId.trim()) return false;
  if (provider.value === "qoder" && purpose.value === "checkin" && !form.refreshToken.trim()) return false;
  if (showToken.value && !form.token.trim()) return false;
  return !showCookie.value || Boolean(form.cookie.trim());
});

function selectProvider(value: Provider): void {
  provider.value = value;
  if (value === "qoder" && purpose.value === "checkin") form.checkinMode = "bearer";
}

function selectPurpose(value: Purpose): void {
  purpose.value = value;
  if (value === "chat") form.checkinMode = "bearer";
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  pending.value = true;
  setMessage("");
  try {
    const result = await apiRequest<ImportResult>(endpoint(), requestOptions());
    clearSecrets();
    confirmCheckinVerification.value = false;
    setMessage("凭据已验证并保存。", false);
    if (result.account) emit("saved", result.account);
  } catch (error) {
    setMessage(String(error), true);
  } finally {
    pending.value = false;
  }
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
    setMessage("OAuth 登录成功，账号已加入代理池。", false);
    emit("saved", result.account);
  } catch (error) {
    setMessage(String(error), true);
  } finally {
    polling.value = false;
  }
}

function endpoint(): string {
  if (provider.value === "codebuddy") return purpose.value === "chat" ? "/auth/codebuddy/manual" : "/auth/codebuddy/checkin";
  return purpose.value === "chat" ? "/auth/qoder/chat" : "/auth/qoder/checkin";
}

function requestOptions(): RequestInit {
  const body: Record<string, string | undefined> = { label: form.label || undefined, account_id: form.accountId || undefined };
  if (provider.value === "qoder" && purpose.value === "chat") body.pat = form.token;
  else if (provider.value === "qoder") Object.assign(body, { access_token: form.token, refresh_token: form.refreshToken });
  else if (purpose.value === "chat") body.token = form.token;
  else Object.assign(body, { mode: form.checkinMode, access_token: form.token || undefined, cookie: form.cookie || undefined });
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function requestSubmit(): void {
  if (provider.value === "codebuddy" && purpose.value === "checkin") {
    confirmCheckinVerification.value = true;
    return;
  }
  void submit();
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
    <div class="segmented-control" aria-label="用途"><button type="button" :class="{ active: purpose === 'chat' }" @click="selectPurpose('chat')">代理请求</button><button type="button" :class="{ active: purpose === 'checkin' }" @click="selectPurpose('checkin')">每日签到</button></div>
    <div class="form-grid">
      <label>显示名称<input v-model="form.label" aria-label="显示名称" autocomplete="off" placeholder="例如：主账号" /></label>
      <label v-if="requiresAccountId">已有账号 ID<span class="required-mark">必填</span><input v-model="form.accountId" aria-label="账号 ID" autocomplete="off" /></label>
      <label v-if="provider === 'codebuddy' && purpose === 'checkin'">认证模式<select v-model="form.checkinMode" aria-label="签到认证模式"><option value="bearer">Bearer Token</option><option value="cookie">Cookie</option><option value="bearer_cookie">Bearer Token + Cookie</option></select></label>
      <label v-if="showToken" class="form-span">{{ tokenLabel }}<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.token" :aria-label="tokenLabel" type="password" autocomplete="new-password" /></div></label>
      <label v-if="showCookie" class="form-span">WorkBuddy Cookie<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.cookie" aria-label="WorkBuddy Cookie" type="password" autocomplete="new-password" /></div></label>
      <label v-if="provider === 'qoder' && purpose === 'checkin'" class="form-span">Qoder 刷新令牌 <span class="required-mark">必填</span><input v-model="form.refreshToken" aria-label="Qoder 刷新令牌" type="password" autocomplete="new-password" /></label>
    </div>
    <p class="helper-text">原始凭据只用于本次受保护请求；成功后表单立即清空，管理台只显示版本和状态。</p>
    <p v-if="provider === 'codebuddy' && purpose === 'checkin'" class="form-message">验证会发送一次 WorkBuddy 每日签到请求；未签到时可能立即领取当天积分。</p>
    <div class="form-actions">
      <button v-if="provider === 'codebuddy' && purpose === 'chat'" class="secondary-button" type="button" :disabled="pending" @click="startOAuth"><LogIn :size="16" />浏览器登录</button>
      <button type="button" :disabled="pending || !canSubmit" @click="requestSubmit"><LoaderCircle v-if="pending" class="spin" :size="16" /><Link v-else :size="16" />{{ provider === 'codebuddy' && purpose === 'checkin' ? '验证并启用' : '验证并保存' }}</button>
      <button v-if="flow" class="secondary-button" type="button" :disabled="polling" @click="pollOAuth"><RefreshCcw :class="{ spin: polling }" :size="16" />继续 OAuth 登录</button>
    </div>
    <p v-if="flow" class="helper-text">流程将在 {{ new Date(flow.expires_at).toLocaleTimeString() }} 过期；可离开此页后返回继续轮询。</p>
    <p v-if="message" class="form-message" :class="{ 'text-danger': failed }" role="status"><CheckCircle2 v-if="!failed" :size="15" />{{ message }}</p>
    <ConfirmDialog :open="confirmCheckinVerification" title="验证并启用 WorkBuddy 签到？" description="系统将发送一次 WorkBuddy 每日签到请求；未签到时可能立即领取当天积分。仅在成功或已签到后才会启用定时签到。" confirm-label="确认并验证" :busy="pending" @cancel="confirmCheckinVerification = false" @confirm="submit" />
  </section>
</template>
