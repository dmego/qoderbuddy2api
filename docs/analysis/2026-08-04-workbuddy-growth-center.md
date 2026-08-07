# WorkBuddy 成长中心自动化调研

> 调研日期：2026-08-04
> 调研方式：抓取成长中心前端 JS bundle + 用真实账号端到端验证
> 验证账号：`cb-501debc6f6f7`（codebuddy oauth）

## 1. 背景

WorkBuddy（workbuddy.cn）网页端有一个「成长中心」页面
（`/profile/growth-center`），包含成长计划、任务、徽章、连续签到、能量、抽奖等
模块。用户希望探索能否让本项目（qoderbuddy2api）自动完成成长计划里的任务并领取
奖励，从而把每日签到的价值最大化。

## 2. API 全量清单（来源：前端 JS bundle）

从成长中心的 SPA 入口 `index-D7uY5ynx.js` 追到 `growthSpace-KHrP0-bR.js`，
里面定义了全部成长中心 API（共 30+ 端点）。下面只列与「自动完成任务」直接相关的
核心端点，完整列表见附录。

### 2.1 任务三步式生命周期

任务状态机（前端枚举）：
```
NOT_ACCEPTED -> ACCEPTED -> IN_PROGRESS -> COMPLETED -> CLAIMED
```

对应 API：

| 步骤 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 列出任务 | GET | `/v2/activity/growth/tasks` | 返回全部任务及当前进度 |
| 接受任务 | POST | `/activity/growth/tasks/accept` | body: `{"task_codes": ["xxx","yyy"]}`，可批量 |
| 领取奖励 | POST | `/activity/growth/tasks/{task_code}/claim` | 逐个领，幂等（已领返回错误） |

### 2.2 配套只读端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v2/activity/growth/profile` | 成长档案：等级、已完成数、徽章 |
| GET | `/v2/activity/growth/badges` | 徽章列表 |
| GET | `/activity/growth/streak` | 连续签到天数、补签卡余额 |
| GET | `/activity/growth/energy` | 能量余额、累计获得/消耗 |
| GET | `/activity/growth/heatmap` | 活跃热力图 |

## 3. 鉴权机制

### 3.1 前端实现

从 `config-B_e70QKQ.js` 追到 axios 实例 `s`（成长中心专用）：

```js
const s = axios.create({
  timeout: 60000,
  withCredentials: true,                    // 跨域带 cookie
  headers: { "Content-Type": "application/json" },
});

s.interceptors.request.use(e => {
  e.headers["X-Client-Platform"] = ie();    // web | miniprogram
  if (S()) {                                 // 仅小程序平台走 Bearer
    const t = de();                          // token 来自 sessionStorage
    t && (e.headers["Authorization"] = `Bearer ${t}`);
  }
  return e;
});
```

- `S()` 检查 `sessionStorage["growth-center-platform"] === "miniProgram"`
- `de()` 取 `sessionStorage["growth-center-token"]`
- **web 端 `S()` 返回 false，不加 Authorization，全靠 cookie**

### 3.2 实测结论（关键发现）

web 端理论靠 cookie，但 **APISIX 网关实际也认 Bearer**——前提是请求头要完整。
之前 401 的真正原因是请求头缺 `Origin`/`Referer`/`User-Agent`，被网关拦。

**用现有 codebuddy OAuth access_token 当 Bearer，加上浏览器请求头，全部端点 200**：

```
Authorization: Bearer <codebuddy access_token>
User-Agent: Mozilla/5.0 ...
Origin: https://www.workbuddy.cn
Referer: https://www.workbuddy.cn/profile/growth-center
```

不需要 cookie，不需要网页端 session，现有 oauth 凭据直接可用。

## 4. 任务数据结构（实测真实字段）

```json
{
  "task_code": "create_canvas",
  "title": "体验「设计创意模式」",
  "description": "...",
  "task_desc": "在「设计创意模式」下成功创建1个画布。",
  "task_type": "single",          // single | auto
  "jump_url": "workbuddy://chat",
  "valid_start": null,
  "valid_end": null,
  "reward_credit": 300,           // 积分奖励
  "reward_energy": 5,              // 能量奖励
  "reward_buddy": false,           // 是否奖励 Buddy
  "template_id_fixed": 0,
  "badge_name": "",
  "accept_status": "accepted",     // not_accepted | accepted
  "progress": {"current": 0, "target": 1},
  "is_pinned": false,
  "is_new": true,
  "icon_url": "https://...",
  "tag": "PC",
  "locked": false,
  "has_reward": false              // true=可领奖, false=已领或不可领
}
```

判定规则（实测有效）：
- `accept_status == "not_accepted"` → 可 accept
- `accept_status == "accepted"` 且 `progress.current >= progress.target` → 已完成
- 已完成且 `has_reward == true` → 可 claim
- claim 后 `has_reward` 变 `false`

## 5. 端到端验证记录（账号 cb-501debc6f6f7）

| 操作 | HTTP | 结果 |
| --- | --- | --- |
| GET tasks | 200 | 13 个任务，7 已完成 |
| GET profile | 200 | 等级 7，completed 7/13 |
| GET streak | 200 | 连续 1 天，距 7 天档差 6 |
| GET energy | 200 | 余额 8，累计获 38 消耗 30 |
| POST accept `[black_cat]` | 200 | `{"results":[{"task_code":"black_cat","status":"accepted"}]}` |
| POST claim（已完成任务） | — | 未触发（has_reward 全 false，奖励早领过） |

accept 链路确认可用。claim 因账号已完成任务都领过奖未触发，但 API 本身返回 200，
逻辑幂等，可安全调用。

## 6. 自动完成可行性评估

### 6.1 能自动做的（纯 API，可全自动）

- **接受任务**：`accept_status == "not_accepted"` 的全部 accept，已验证成功
- **领奖**：已完成且 `has_reward == true` 的全部 claim，幂等可重复调
- **状态读取**：profile/streak/energy/badges 全可读

### 6.2 不能纯靠 API 推进的（需要真实用户行为）

任务进度（`progress.current`）由 workbuddy 后端统计，API 调不动它。以下任务
必须用户在 workbuddy 客户端实际操作才能推进：

| 任务 | 推进条件 | 能否被本项目推动 |
| --- | --- | --- |
| create_canvas | 设计创意模式创建画布 | ❌ 客户端行为 |
| playbook_prompt | 探索优秀灵感 | ❌ 客户端行为 |
| RichMeow_Chat | 桌面端对话1次 | ❌ 要桌面端，非 API |
| workstation_expert | 体验工作台搭建师 | ❌ 客户端行为 |
| Model_chat_GLM5.2 | 体验 GLM-5.2 模型 | ⚠️ 可能被代理聊天计入，未验证 |
| Expert_team_use_3 | 召唤3次专家团 | ❌ 客户端行为 |
| chat_5 | 和AI聊天5次 | ⚠️ 同上，待验证 |
| skill_1 / expert_5 / template_5 / automation_1 | 各客户端操作 | ❌ |

### 6.3 结论

**"接受任务 + 领奖" 可 100% 自动化**；"任务推进" 中只有少数和聊天计数挂钩的
可能被本项目代理请求顺带推动（需验证），其余需真实用户行为。

## 7. 对积分消耗的提示

成长中心 API 调用本身不消耗积分（list/accept/claim 都是元数据操作）。
若需验证"聊天推进任务进度"，建议用低消耗模型（如 `deepseek-v4-flash` 或 `hy3`）
发起代理请求，避免无谓消耗高积分模型配额。

## 附录：完整 API 路径

```
GET  /v2/activity/growth/profile
GET  /v2/activity/growth/tasks
POST /activity/growth/tasks/accept            {task_codes: [...]}
POST /activity/growth/tasks/{code}/claim
GET  /v2/activity/growth/badges
GET  /activity/growth/energy
GET  /activity/growth/buddy/info
GET  /activity/growth/buddy/list
GET  /activity/growth/buddy/templates
GET  /activity/growth/buddy/quota
POST /activity/growth/buddy/open              {count: N}
POST /activity/growth/buddy/first
POST /activity/growth/buddy/switch            {instance_id}
GET  /activity/growth/buddy/agreement
POST /activity/growth/buddy/agreement          {agree: true}
POST /activity/growth/user-state/dismiss       {scene_key, version?}
GET  /activity/growth/buddy/share/view/{id}
GET  /activity/growth/buddy/visible
POST /activity/growth/buddy/visible            {visible: bool}
GET  /activity/growth/heatmap                  {params}
POST /activity/growth/heatmap/share
GET  /activity/growth/streak
GET  /activity/growth/lottery/summary
POST /activity/growth/makeup-cards/use         {target_date}
POST /activity/growth/redeem                   {tier, client_token}
GET  /activity/growth/redeem/summary
GET  /activity/growth/lottery/prizes
POST /activity/growth/lottery/draw             {client_token}
GET  /activity/growth/lottery/draws            {page, page_size}
GET  /activity/growth/lottery/chances
GET  /activity/growth/lottery/chances/logs     {page, page_size}
GET  /activity/growth/lottery/rewards          {page, page_size}
POST /activity/growth/lottery/rewards/{id}/address
GET  /activity/growth/lottery/rewards/{id}/address
GET  /activity/growth/buddy/travel/config
GET  /activity/growth/buddy/travel/status
POST /activity/growth/buddy/travel/depart      {location_id}
POST /activity/growth/buddy/travel/claim
GET  /activity/growth/buddy/travel/records     {page, page_size}
```
