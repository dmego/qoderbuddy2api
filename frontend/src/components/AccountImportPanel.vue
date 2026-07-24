<script setup lang="ts">
import { KeyRound, Link, LoaderCircle, LogIn } from "@lucide/vue";
import { reactive, ref } from "vue";

import { apiRequest } from "@/api/client";

const emit = defineEmits<{ saved: [] }>();
const mode = ref<"codebuddy" | "qoder">("codebuddy");
const pending = ref(false);
const message = ref("");
const form = reactive({ label: "", token: "", accountId: "", refreshToken: "" });

async function submitManual(): Promise<void> {
  pending.value = true;
  message.value = "";
  try {
    if (mode.value === "codebuddy") {
      await apiRequest("/auth/codebuddy/manual", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: form.label || "CodeBuddy", token: form.token }) });
    } else if (form.accountId && form.refreshToken) {
      await apiRequest("/auth/qoder/checkin", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_id: form.accountId, access_token: form.token, refresh_token: form.refreshToken }) });
    } else {
      await apiRequest("/auth/qoder/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: form.label || "Qoder", pat: form.token, account_id: form.accountId || undefined }) });
    }
    Object.assign(form, { label: "", token: "", accountId: "", refreshToken: "" });
    message.value = "凭据已验证并保存。";
    emit("saved");
  } catch (error) { message.value = String(error); }
  finally { pending.value = false; }
}

async function oauth(): Promise<void> {
  pending.value = true;
  message.value = "正在创建登录流程";
  try {
    const flow = await apiRequest<{ flow_id: string; auth_url: string }>("/auth/codebuddy/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: form.label || "CodeBuddy OAuth" }) });
    window.open(flow.auth_url, "_blank", "noopener,noreferrer");
    for (let attempt = 0; attempt < 150; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
      const result = await apiRequest<{ status: string }>("/auth/codebuddy/poll", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ flow_id: flow.flow_id }) });
      if (result.status === "pending") continue;
      if (result.status !== "success") throw new Error("登录未完成");
      message.value = "登录成功，账号已加入代理池。";
      emit("saved");
      return;
    }
    throw new Error("登录流程超时");
  } catch (error) { message.value = String(error); }
  finally { pending.value = false; }
}
</script>

<template>
  <section class="import-panel">
    <div class="segmented-control" aria-label="账号类型"><button type="button" :class="{ active: mode === 'codebuddy' }" @click="mode = 'codebuddy'">CodeBuddy</button><button type="button" :class="{ active: mode === 'qoder' }" @click="mode = 'qoder'">Qoder</button></div>
    <div class="form-grid">
      <label>显示名称<input v-model="form.label" autocomplete="off" placeholder="例如：主账号" /></label>
      <label v-if="mode === 'qoder'">已有账号 ID（可选）<input v-model="form.accountId" autocomplete="off" placeholder="导入签到凭据时必填" /></label>
      <label class="form-span">{{ mode === 'qoder' ? 'PAT / Access Token' : 'Bearer Token' }}<div class="input-with-icon"><KeyRound :size="16" /><input v-model="form.token" type="password" autocomplete="new-password" /></div></label>
      <label v-if="mode === 'qoder'" class="form-span">Refresh Token（仅签到凭据）<input v-model="form.refreshToken" type="password" autocomplete="new-password" /></label>
    </div>
    <div class="form-actions">
      <button v-if="mode === 'codebuddy'" class="secondary-button" type="button" :disabled="pending" @click="oauth"><LogIn :size="16" />浏览器登录</button>
      <button type="button" :disabled="pending || !form.token" @click="submitManual"><LoaderCircle v-if="pending" class="spin" :size="16" /><Link v-else :size="16" />验证并保存</button>
    </div>
    <p v-if="message" class="form-message" role="status">{{ message }}</p>
  </section>
</template>
