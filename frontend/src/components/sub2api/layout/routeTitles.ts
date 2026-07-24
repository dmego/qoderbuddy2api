/* SPDX-License-Identifier: LGPL-3.0-or-later
 * Derived from Wei-Shaw/sub2api frontend layout route-title behavior at cb24522.
 */
const titles: Record<string, string> = {
  overview: "运行总览", service: "代理服务", accounts: "账号管理", "account-add": "添加账号", "account-detail": "账号详情",
  credentials: "凭据管理", "proxy-keys": "代理密钥", models: "模型管理", usage: "用量监控", checkin: "签到中心", settings: "运行设置", audit: "审计与备份",
};

export function routeTitle(name: unknown): string {
  return typeof name === "string" ? titles[name] ?? "2api 控制台" : "2api 控制台";
}
