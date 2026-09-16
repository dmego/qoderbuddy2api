import { expect, test, type Page } from "@playwright/test";

const adminKey = "playwright-admin-key";

async function login(page: Page): Promise<void> {
  await page.goto("/admin/login");
  await expect(page.getByRole("heading", { name: "管理员登录" })).toBeVisible();
  await expect(page.getByLabel("远程 HTTP 安全提示")).toHaveCount(0);
  await page.getByLabel("管理员密钥（Admin Key）").fill(adminKey);
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(page).toHaveURL(/\/admin\/overview$/);
}

test.describe.serial("2api Control Plane", () => {
  test("登录不持久化密钥并展示完整管理导航", async ({ page }) => {
    await login(page);
    await expect(page.getByRole("complementary", { name: "主导航" })).toBeVisible();
    await expect(page.getByRole("link", { name: "代理服务" })).toBeVisible();
    await expect(page.getByRole("link", { name: "代理密钥" })).toBeVisible();
    await expect(page.getByRole("link", { name: "审计" })).toBeVisible();
    await expect(page.locator("main")).toContainText("运行总览");
    expect(await page.evaluate(() => ({ ...localStorage, ...sessionStorage }))).toEqual({});
  });

  test("主要管理页面可访问且显示安全空状态", async ({ page }) => {
    await login(page);
    const pages = [
      ["账号", "账号管理"],
      ["积分监控", "积分监控"],
      ["凭据", "凭据管理"],
      ["代理密钥", "代理密钥"],
      ["模型", "模型管理"],
      ["用量", "用量监控"],
      ["签到", "签到中心"],
      ["设置", "运行设置"],
      ["审计", "审计与备份"],
    ] as const;
    for (const [link, heading] of pages) {
      await page.getByRole("link", { name: link }).click();
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    }
  });

  test("窄屏导航显式展开所有管理入口并在跳转后收起", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    const toggle = page.getByRole("button", { name: "展开导航" });
    await expect(toggle).toBeVisible();
    await toggle.click();
    await expect(page.getByRole("button", { name: "收起导航" })).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator(".sidebar")).toHaveCSS("transform", "matrix(1, 0, 0, 1, 0, 0)");
    await expect(page.getByRole("link", { name: "审计" })).toBeVisible();
    await page.getByRole("link", { name: "审计" }).click();
    await expect(page.getByRole("heading", { name: "审计与备份" })).toBeVisible();
    await expect(page.getByRole("button", { name: "展开导航" })).toBeVisible();
  });
});
