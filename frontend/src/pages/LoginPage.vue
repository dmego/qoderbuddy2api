<script setup lang="ts">
import { KeyRound, LockKeyhole, TriangleAlert } from "@lucide/vue";
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useSessionStore } from "@/stores/session";

const router = useRouter();
const session = useSessionStore();
const adminKey = ref("");
const pending = ref(false);
const error = ref("");

async function login(): Promise<void> {
  pending.value = true;
  error.value = "";
  try {
    const response = await fetch("/api/admin/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ admin_key: adminKey.value }),
    });
    if (!response.ok) {
      error.value = "管理密钥无效或当前连接不符合安全要求。";
      return;
    }
    const payload = (await response.json()) as { csrf_token?: string };
    if (!payload.csrf_token) throw new Error("missing csrf token");
    session.establish(payload.csrf_token);
    await router.replace("/overview");
  } catch {
    error.value = "无法建立管理会话，请检查网络和 Cookie 安全配置。";
  } finally {
    pending.value = false;
    adminKey.value = "";
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-panel" aria-labelledby="login-title">
      <div class="login-brand"><span>2</span> 2api 管理控制台</div>
      <div class="login-copy">
        <LockKeyhole :size="24" aria-hidden="true" />
        <h1 id="login-title">管理员登录</h1>
        <p>使用独立管理员密钥进入本机代理控制台。</p>
      </div>
      <form @submit.prevent="login">
        <label for="admin-key">管理员密钥（Admin Key）</label>
        <div class="input-with-icon">
          <KeyRound :size="17" aria-hidden="true" />
          <input
            id="admin-key"
            v-model="adminKey"
            type="password"
            autocomplete="current-password"
            required
          />
        </div>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button type="submit" :disabled="pending || !adminKey">
          {{ pending ? "正在验证" : "登录控制台" }}
        </button>
      </form>
      <aside class="transport-warning" aria-label="远程 HTTP 安全提示">
        <TriangleAlert :size="18" aria-hidden="true" />
        <div>
          <strong>受信局域网 HTTP 是显式降级模式</strong>
          <p>
            只有在 Mac mini 的 Tailscale 或受信 LAN 内无法部署 HTTPS 时，才配置
            <code>QB2API_ADMIN_COOKIE_SECURE=false</code>。此时密钥和会话不受 TLS
            保护，禁止暴露到公网。
          </p>
        </div>
      </aside>
      <p class="security-note">管理员密钥只用于建立 HttpOnly 会话，密钥不会写入浏览器存储。</p>
    </section>
  </main>
</template>

<style scoped src="../styles/login.css"></style>
