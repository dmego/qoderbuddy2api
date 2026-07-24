/**
 * 2api admin SPA (hash routes). credentials include + CSRF on mutations.
 */

const state = {
  // design: do not put CSRF/secrets into sessionStorage/localStorage
  csrf: "",
  authed: false,
};

function $(sel, root = document) {
  return root.querySelector(sel);
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    Accept: "application/json",
    ...(options.headers || {}),
  };
  let body = options.body;
  if (body && typeof body === "object" && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && state.csrf) {
    headers["X-CSRF-Token"] = state.csrf;
  }
  const res = await fetch(path, {
    method,
    headers,
    body,
    credentials: "include",
  });
  let data = null;
  const text = await res.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  return { ok: res.ok, status: res.status, data };
}

function setAuthChrome(authed) {
  state.authed = authed;
  $("#nav").classList.toggle("hidden", !authed);
  $("#logout-btn").classList.toggle("hidden", !authed);
  document.querySelectorAll("#nav a").forEach((a) => {
    const route = a.getAttribute("data-route");
    a.classList.toggle("active", route === currentRoute());
  });
}

function currentRoute() {
  const h = location.hash.replace(/^#/, "") || "/";
  return h.startsWith("/") ? h : `/${h}`;
}

function badge(status) {
  const s = String(status || "");
  let cls = "";
  if (["active", "CLAIMED", "ALREADY_CHECKED_IN", "ok", "finished"].includes(s)) cls = "ok";
  else if (["action_required", "needs_reauth", "needs_import", "pending"].includes(s)) cls = "warn";
  else if (["disabled", "FAILED", "failed", "invalid"].includes(s)) cls = "err";
  return `<span class="badge ${cls}">${escapeHtml(s)}</span>`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function ensureSession() {
  const { ok, data } = await api("/api/admin/session");
  if (ok && data?.authenticated) {
    if (data.csrf_token) state.csrf = data.csrf_token;
    setAuthChrome(true);
    return true;
  }
  setAuthChrome(false);
  return false;
}

function renderLogin(main) {
  main.innerHTML = `
    <div class="login-wrap card">
      <h1>管理员登录</h1>
      <p class="muted">使用部署配置的 Admin Key。密钥不会写入 localStorage。</p>
      <form id="login-form">
        <div class="field">
          <label for="admin-key">Admin Key</label>
          <input id="admin-key" name="admin_key" type="password" autocomplete="current-password" required />
        </div>
        <p id="login-error" class="error hidden"></p>
        <button class="btn primary" type="submit">登录</button>
      </form>
    </div>`;
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = $("#admin-key").value;
    const err = $("#login-error");
    err.classList.add("hidden");
    const { ok, status, data } = await api("/api/admin/session", {
      method: "POST",
      body: { admin_key: key },
    });
    if (!ok) {
      err.textContent =
        status === 429 ? "登录过于频繁，请稍后再试" : data?.error || "登录失败";
      err.classList.remove("hidden");
      return;
    }
    state.csrf = data.csrf_token || "";
    setAuthChrome(true);
    location.hash = "#/";
    route();
  });
}

async function renderDashboard(main) {
  const [acc, chk] = await Promise.all([
    api("/api/admin/accounts"),
    api("/api/admin/checkin/status"),
  ]);
  if (!acc.ok) {
    main.innerHTML = `<p class="error">加载失败 (${acc.status})</p>`;
    return;
  }
  const accounts = acc.data.accounts || [];
  const active = accounts.filter((a) => a.enabled && !a.shadowed).length;
  const action = accounts.filter((a) => a.summary_status === "action_required").length;
  const chkData = chk.ok ? chk.data : {};
  const daily = chkData.daily_states || [];
  const done = daily.filter((d) =>
    ["CLAIMED", "ALREADY_CHECKED_IN"].includes(d.terminal_outcome)
  ).length;

  main.innerHTML = `
    <h1>概览</h1>
    <div class="grid">
      <div class="stat"><div class="label">账号总数</div><div class="value">${accounts.length}</div></div>
      <div class="stat"><div class="label">可用</div><div class="value">${active}</div></div>
      <div class="stat"><div class="label">需处理</div><div class="value">${action}</div></div>
      <div class="stat"><div class="label">今日签到完成</div><div class="value">${done}</div></div>
    </div>
    <div class="card">
      <div class="row">
        <span class="muted">签到调度</span>
        ${badge(chkData.enabled ? "enabled" : "disabled")}
        <span class="muted mono">${escapeHtml(chkData.checkin_at || "-")} ${escapeHtml(chkData.timezone || "")}</span>
        <span class="muted">下次: ${escapeHtml(chkData.next_run_at || "-")}</span>
        <span class="muted">运行中: ${chkData.running ? "是" : "否"}</span>
      </div>
    </div>`;
}

async function renderAccounts(main) {
  const { ok, data } = await api("/api/admin/accounts");
  if (!ok) {
    main.innerHTML = `<p class="error">加载账号失败</p>`;
    return;
  }
  const rows = (data.accounts || [])
    .map((a) => {
      const chat = a.purposes?.chat?.status || "-";
      const cin = a.purposes?.checkin?.status || "-";
      const key = `${a.provider}/${a.account_id}`;
      const actions = [];
      if (a.source === "env") {
        actions.push(
          `<button class="btn" data-promote="${escapeHtml(key)}" type="button">Promote</button>`
        );
      } else {
        const en = a.enabled !== false;
        actions.push(
          `<button class="btn" data-toggle="${escapeHtml(key)}" data-enabled="${en ? "0" : "1"}" type="button">${en ? "禁用" : "启用"}</button>`
        );
        actions.push(
          `<button class="btn" data-verify="${escapeHtml(key)}" type="button">验证签到</button>`
        );
        actions.push(
          `<button class="btn danger" data-del="${escapeHtml(key)}" type="button">删除</button>`
        );
      }
      return `<tr>
        <td>${escapeHtml(a.provider)}</td>
        <td class="mono">${escapeHtml(a.account_id)}</td>
        <td>${escapeHtml(a.label || "")}</td>
        <td>${escapeHtml(a.source)}${a.shadowed ? " · shadowed" : ""}</td>
        <td>${badge(a.summary_status)}</td>
        <td>${badge(chat)} / ${badge(cin)}</td>
        <td>${escapeHtml(a.masked_identity || "")}</td>
        <td class="row" style="gap:6px;flex-wrap:wrap">${actions.join("")}</td>
      </tr>`;
    })
    .join("");

  main.innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:12px">
      <h1 style="margin:0">账号</h1>
      <a class="btn primary" href="#/accounts/add">添加账号</a>
    </div>
    <div class="card" style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Provider</th><th>ID</th><th>Label</th><th>来源</th>
            <th>状态</th><th>Chat / Checkin</th><th>身份</th><th>操作</th>
          </tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="8" class="muted">暂无账号</td></tr>`}</tbody>
      </table>
    </div>`;

  main.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [provider, account_id] = btn.getAttribute("data-del").split("/");
      if (!confirm(`删除 ${provider}/${account_id}?`)) return;
      const res = await api(`/api/admin/accounts/${provider}/${account_id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        toast(res.data?.detail || "删除失败");
        return;
      }
      toast("已删除");
      renderAccounts(main);
    });
  });
  main.querySelectorAll("[data-promote]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [provider, account_id] = btn.getAttribute("data-promote").split("/");
      const res = await api(`/api/admin/accounts/${provider}/${account_id}/promote`, {
        method: "POST",
        body: {},
      });
      if (!res.ok) {
        toast(res.data?.detail || "promote 失败");
        return;
      }
      toast(`已 promote → ${res.data.account?.account_id || ""}`);
      renderAccounts(main);
    });
  });
  main.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [provider, account_id] = btn.getAttribute("data-toggle").split("/");
      const enabled = btn.getAttribute("data-enabled") === "1";
      const res = await api(`/api/admin/accounts/${provider}/${account_id}`, {
        method: "PATCH",
        body: { enabled },
      });
      if (!res.ok) {
        toast(res.data?.detail || "更新失败");
        return;
      }
      toast(enabled ? "已启用" : "已禁用");
      renderAccounts(main);
    });
  });
  main.querySelectorAll("[data-verify]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [provider, account_id] = btn.getAttribute("data-verify").split("/");
      const res = await api(
        `/api/admin/accounts/${provider}/${account_id}/verify-checkin`,
        { method: "POST", body: {} }
      );
      if (!res.ok) {
        toast(res.data?.detail || res.data?.error || "验证失败");
        return;
      }
      const outcome = res.data.results?.[0]?.outcome || "done";
      toast(`验证结果: ${outcome}`);
      renderAccounts(main);
    });
  });
}

function renderAdd(main) {
  main.innerHTML = `
    <h1>添加账号</h1>
    <div class="tabs" role="tablist">
      <button type="button" class="active" data-tab="cb-oauth">CodeBuddy OAuth</button>
      <button type="button" data-tab="cb-manual">CodeBuddy 手动</button>
      <button type="button" data-tab="qd-chat">Qoder PAT</button>
      <button type="button" data-tab="qd-checkin">Qoder 签到导入</button>
    </div>
    <div id="tab-panels"></div>`;

  const panels = $("#tab-panels");
  const tabs = main.querySelectorAll(".tabs button");

  function show(tab) {
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
    if (tab === "cb-oauth") {
      panels.innerHTML = `
        <div class="card">
          <div class="field">
            <label for="cb-label">Label</label>
            <input id="cb-label" value="codebuddy" />
          </div>
          <button id="cb-start" class="btn primary" type="button">开始 OAuth</button>
          <div id="cb-oauth-result" class="muted" style="margin-top:12px"></div>
        </div>`;
      $("#cb-start").onclick = async () => {
        const label = $("#cb-label").value || "codebuddy";
        const res = await api("/api/admin/auth/codebuddy/start", {
          method: "POST",
          body: { label },
        });
        const box = $("#cb-oauth-result");
        if (!res.ok) {
          box.innerHTML = `<p class="error">${escapeHtml(res.data?.detail || "start failed")}</p>`;
          return;
        }
        const { flow_id, auth_url } = res.data;
        box.innerHTML = `
          <p>在浏览器打开授权页，完成后点轮询：</p>
          <p><a href="${escapeHtml(auth_url)}" target="_blank" rel="noopener">打开授权 URL</a></p>
          <p class="mono">flow: ${escapeHtml(flow_id)}</p>
          <button id="cb-poll" class="btn" type="button">轮询结果</button>
          <div id="cb-poll-msg"></div>`;
        $("#cb-poll").onclick = async () => {
          const p = await api("/api/admin/auth/codebuddy/poll", {
            method: "POST",
            body: { flow_id },
          });
          const msg = $("#cb-poll-msg");
          if (!p.ok) {
            msg.innerHTML = `<p class="error">${escapeHtml(p.data?.detail || "poll failed")}</p>`;
            return;
          }
          if (p.data.status === "pending") {
            msg.textContent = "等待授权…";
            return;
          }
          if (p.data.status === "success") {
            msg.innerHTML = `<p class="muted">成功：${escapeHtml(p.data.account?.account_id || "")}</p>`;
            toast("CodeBuddy 账号已添加");
            return;
          }
          msg.innerHTML = `<p class="error">${escapeHtml(p.data.message || "failed")}</p>`;
        };
      };
    } else if (tab === "cb-manual") {
      panels.innerHTML = `
        <div class="card">
          <div class="field"><label for="cbm-label">Label</label><input id="cbm-label" value="manual" /></div>
          <div class="field"><label for="cbm-token">Bearer Token</label><input id="cbm-token" type="password" autocomplete="off" /></div>
          <button id="cbm-save" class="btn primary" type="button">保存</button>
        </div>`;
      $("#cbm-save").onclick = async () => {
        const res = await api("/api/admin/auth/codebuddy/manual", {
          method: "POST",
          body: { label: $("#cbm-label").value, token: $("#cbm-token").value },
        });
        if (!res.ok) {
          toast(res.data?.detail || "保存失败");
          return;
        }
        toast("已保存");
        location.hash = "#/accounts";
      };
    } else if (tab === "qd-chat") {
      panels.innerHTML = `
        <div class="card">
          <div class="field"><label for="qd-label">Label</label><input id="qd-label" value="qoder" /></div>
          <div class="field"><label for="qd-pat">PAT (pt_…)</label><input id="qd-pat" type="password" autocomplete="off" /></div>
          <button id="qd-save" class="btn primary" type="button">导入 Chat PAT</button>
        </div>`;
      $("#qd-save").onclick = async () => {
        const res = await api("/api/admin/auth/qoder/chat", {
          method: "POST",
          body: { label: $("#qd-label").value, pat: $("#qd-pat").value },
        });
        if (!res.ok) {
          toast(res.data?.detail || "导入失败");
          return;
        }
        toast("Qoder chat 已导入");
        location.hash = "#/accounts";
      };
    } else {
      panels.innerHTML = `
        <div class="card">
          <p class="muted">将 Windows exporter 的 access/refresh 绑定到已有 Qoder 账号。</p>
          <div class="field"><label for="qdc-id">account_id</label><input id="qdc-id" class="mono" placeholder="qd-…" /></div>
          <div class="field"><label for="qdc-access">access_token</label><input id="qdc-access" type="password" autocomplete="off" /></div>
          <div class="field"><label for="qdc-refresh">refresh_token</label><input id="qdc-refresh" type="password" autocomplete="off" /></div>
          <button id="qdc-save" class="btn primary" type="button">验证并导入</button>
        </div>`;
      $("#qdc-save").onclick = async () => {
        const res = await api("/api/admin/auth/qoder/checkin", {
          method: "POST",
          body: {
            account_id: $("#qdc-id").value,
            access_token: $("#qdc-access").value,
            refresh_token: $("#qdc-refresh").value,
          },
        });
        if (!res.ok) {
          toast(res.data?.detail || "导入失败");
          return;
        }
        toast("签到凭据已导入");
        location.hash = "#/accounts";
      };
    }
  }

  tabs.forEach((t) => t.addEventListener("click", () => show(t.dataset.tab)));
  show("cb-oauth");
}

async function renderCheckin(main) {
  const { ok, data } = await api("/api/admin/checkin/status");
  if (!ok) {
    main.innerHTML = `<p class="error">加载签到状态失败</p>`;
    return;
  }
  const eligible = (data.eligible_accounts || [])
    .map(
      (a) =>
        `<tr><td>${escapeHtml(a.provider)}</td><td class="mono">${escapeHtml(a.account_id)}</td><td>${badge(a.status)}</td></tr>`
    )
    .join("");
  const daily = (data.daily_states || [])
    .map(
      (d) =>
        `<tr><td>${escapeHtml(d.provider)}</td><td class="mono">${escapeHtml(d.account_id)}</td><td>${badge(d.terminal_outcome || "-")}</td><td class="mono">${escapeHtml(d.updated_at || "")}</td></tr>`
    )
    .join("");

  main.innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:12px">
      <h1 style="margin:0">签到</h1>
      <button id="run-checkin" class="btn primary" type="button" ${data.running ? "disabled" : ""}>立即执行</button>
    </div>
    <div class="card">
      <div class="row">
        ${badge(data.enabled ? "enabled" : "disabled")}
        <span class="muted mono">${escapeHtml(data.local_date || "")} ${escapeHtml(data.timezone || "")}</span>
        <span class="muted">计划 ${escapeHtml(data.checkin_at || "-")}</span>
        <span class="muted">下次 ${escapeHtml(data.next_run_at || "-")}</span>
        <span class="muted">running=${data.running ? "true" : "false"}</span>
      </div>
    </div>
    <h2>可签到账号</h2>
    <div class="card" style="overflow-x:auto">
      <table><thead><tr><th>Provider</th><th>ID</th><th>Status</th></tr></thead>
      <tbody>${eligible || `<tr><td colspan="3" class="muted">无</td></tr>`}</tbody></table>
    </div>
    <h2>今日状态</h2>
    <div class="card" style="overflow-x:auto">
      <table><thead><tr><th>Provider</th><th>ID</th><th>Outcome</th><th>Updated</th></tr></thead>
      <tbody>${daily || `<tr><td colspan="4" class="muted">暂无记录</td></tr>`}</tbody></table>
    </div>`;

  $("#run-checkin").onclick = async () => {
    const res = await api("/api/admin/checkin/run", { method: "POST", body: {} });
    if (res.status === 409) {
      toast("签到正在进行中");
      return;
    }
    if (!res.ok) {
      toast(res.data?.detail || "执行失败");
      return;
    }
    toast(`完成 run ${res.data.run_id?.slice(0, 8) || ""}`);
    renderCheckin(main);
  };
}

/** 只读设置页（设计 §7.3 /admin/settings）— 不提供 runtime 写入 */
async function renderSettings(main) {
  const { ok, data } = await api("/api/admin/settings");
  if (!ok) {
    main.innerHTML = `<p class="error">加载设置失败</p>`;
    return;
  }

  function flatten(obj, prefix = "") {
    const out = [];
    if (obj == null || typeof obj !== "object") {
      out.push([prefix || "(root)", obj]);
      return out;
    }
    // {value, source, restart_required_to_change}
    if ("value" in obj && "source" in obj) {
      out.push([
        prefix,
        `${JSON.stringify(obj.value)}  ·  source=${obj.source}${
          obj.restart_required_to_change ? "  ·  restart" : ""
        }`,
      ]);
      return out;
    }
    for (const [k, v] of Object.entries(obj)) {
      const key = prefix ? `${prefix}.${k}` : k;
      out.push(...flatten(v, key));
    }
    return out;
  }

  const rows = flatten(data)
    .map(
      ([k, v]) =>
        `<tr><td class="mono">${escapeHtml(k)}</td><td class="mono">${escapeHtml(
          String(v)
        )}</td></tr>`
    )
    .join("");
  main.innerHTML = `
    <h1>设置</h1>
    <p class="muted">只读展示当前进程生效配置（source=env/default）。修改环境变量后需重启服务；无 runtime 写入 API。</p>
    <div class="card" style="overflow-x:auto">
      <table>
        <thead><tr><th>项</th><th>值</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="2" class="muted">无数据</td></tr>`}</tbody>
      </table>
    </div>`;
}

async function route() {
  const main = $("#main");
  const path = currentRoute();
  const authed = await ensureSession();
  if (!authed) {
    renderLogin(main);
    return;
  }
  setAuthChrome(true);
  if (path === "/" || path === "") await renderDashboard(main);
  else if (path === "/accounts") await renderAccounts(main);
  else if (path === "/accounts/add") renderAdd(main);
  else if (path === "/checkin") await renderCheckin(main);
  else if (path === "/settings") await renderSettings(main);
  else main.innerHTML = `<p class="muted">未知页面</p>`;
}

$("#logout-btn").addEventListener("click", async () => {
  await api("/api/admin/session/logout", { method: "POST" });
  state.csrf = "";
  setAuthChrome(false);
  location.hash = "#/";
  route();
});

window.addEventListener("hashchange", route);
route();
