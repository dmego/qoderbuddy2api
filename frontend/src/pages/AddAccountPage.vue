<script setup lang="ts">
import { ArrowLeft, ShieldCheck } from "@lucide/vue";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import AccountImportPanel, { type AccountReference } from "@/components/AccountImportPanel.vue";

const route = useRoute();
const router = useRouter();
const provider = computed(() => route.query.provider === "qoder" ? "qoder" : "codebuddy");
const purpose = computed(() => route.query.purpose === "checkin" ? "checkin" : "chat");
const accountId = computed(() => typeof route.query.accountId === "string" ? route.query.accountId : "");
const label = computed(() => typeof route.query.label === "string" ? route.query.label : "");

function saved(account: AccountReference): void {
  void router.replace({ name: "account-detail", params: { provider: account.provider, accountId: account.account_id } });
}
</script>

<template>
  <section class="page-content">
    <header class="page-header"><div><h1>{{ accountId ? "重新授权账号" : "添加账号" }}</h1><p>选择 Provider 与用途，服务端会先执行可验证的凭据检查，再以事务方式保存加密材料。</p></div><button class="secondary-button" type="button" @click="router.push({ name: 'accounts' })"><ArrowLeft :size="16" />返回账号池</button></header>
    <div class="security-banner"><ShieldCheck :size="18" /><div><strong>凭据不会回显</strong><span>浏览器仅提交当前表单；保存后页面只会获得账号状态、用途与凭据版本元数据。</span></div></div>
    <AccountImportPanel :provider="provider" :purpose="purpose" :account-id="accountId" :label="label" @saved="saved" />
  </section>
</template>
