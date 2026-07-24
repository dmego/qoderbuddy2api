import { VueQueryPlugin } from "@tanstack/vue-query";
import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";
import { useUiStore } from "./stores/ui";
import "./styles/main.css";

const app = createApp(App);
const pinia = createPinia();
app.use(pinia).use(VueQueryPlugin).use(router);
useUiStore(pinia).initializeTheme();
app.mount("#app");
