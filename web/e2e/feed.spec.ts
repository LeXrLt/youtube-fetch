import { expect, test } from "@playwright/test";

import { getWebRouteKey, withWebBasePath } from "../lib/web-route";

test("requires the configured route key", async ({ request }) => {
  const rootResponse = await request.get("/");
  expect(rootResponse.status()).toBe(404);
  expect(await rootResponse.text()).not.toContain(getWebRouteKey());

  const unprefixedResponse = await request.get("/subtitles");
  expect(unprefixedResponse.status()).toBe(404);
  expect(await unprefixedResponse.text()).not.toContain(getWebRouteKey());

  const protectedResponse = await request.get(withWebBasePath("/"));
  expect(protectedResponse.status()).toBe(200);
});

test("navigates the live feeds and expands a transcript", async ({ page }) => {
  await page.goto(withWebBasePath("/"));

  await expect(page.getByRole("heading", { name: "首页", level: 1 })).toBeVisible();
  const firstPost = page.locator("article.transcript-post").first();
  await expect(firstPost).toBeVisible();
  await expect(firstPost.getByText("简体中文", { exact: true })).toBeVisible();

  const transcript = firstPost.locator(".transcript-copy");
  await expect(transcript).toHaveClass(/is-collapsed/);
  await firstPost.getByRole("button", { name: "展开全文" }).click();
  await expect(transcript).not.toHaveClass(/is-collapsed/);

  await page.getByRole("link", { name: "字幕", exact: true }).click();
  await expect(page).toHaveURL(/\/subtitles$/);
  await expect(page.getByRole("heading", { name: "字幕", level: 1 })).toBeVisible();
  await expect(page.locator("article.transcript-post").first()).toBeVisible();
});

test("filters tags and opens the corresponding original transcript", async ({ page }) => {
  await page.goto(withWebBasePath("/tags"));

  await expect(page.getByRole("heading", { name: "标签", level: 1 })).toBeVisible();
  const firstCard = page.locator(".tag-card").first();
  await expect(firstCard).toBeVisible();
  const firstName = (await firstCard.locator(".tag-card-name span").innerText()).trim();

  await page.getByRole("searchbox", { name: "筛选标签" }).fill(firstName);
  await expect(page.locator(".tag-card").first()).toContainText(firstName);
  await page.locator(".tag-card").first().click();

  await expect(page.getByRole("heading", { name: `#${firstName}`, level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "原字幕", level: 2 })).toBeVisible();
  await expect(page.locator("article.transcript-post").first()).toBeVisible();
});

test("shows the stored channel profile without horizontal overflow", async ({ page }) => {
  await page.goto(withWebBasePath("/"));

  const firstAuthor = page.locator(".post-author").first();
  await expect(firstAuthor).toBeVisible();
  await firstAuthor.click();

  await expect(page.locator(".channel-profile-name")).toBeVisible();
  await expect(page.locator(".channel-description")).not.toBeEmpty();
  await expect(page.locator(".channel-profile .avatar img")).toBeVisible();

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("manages channels and reports an unknown channel inline", async ({ page }) => {
  await page.goto(withWebBasePath("/channels"));

  await expect(page.getByRole("heading", { name: "频道", level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "频道", exact: true })).toHaveAttribute(
    "aria-current",
    "page",
  );

  const firstChannel = page.locator(".channel-management-row").first();
  await expect(firstChannel).toBeVisible();
  await expect(firstChannel.getByRole("switch")).toBeVisible();
  await expect(firstChannel.locator(".channel-management-id")).toContainText(/^UC/);

  await page
    .getByRole("textbox", { name: "频道链接或用户 ID" })
    .fill("invalid channel reference");
  await page.getByRole("button", { name: "确认" }).click();
  await expect(page.locator("#channel-add-message")).toHaveText("频道不存在！");

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("keeps searches under the protected route", async ({ page }) => {
  await page.goto(withWebBasePath("/"));

  const query = "route-prefix-check";
  await page.getByRole("searchbox", { name: "搜索字幕或博主" }).fill(query);
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.waitForURL((url) => url.searchParams.get("q") === query);

  const currentUrl = new URL(page.url());
  expect(currentUrl.pathname).toBe(withWebBasePath("/"));
  await expect(page.getByRole("heading", { name: "首页", level: 1 })).toBeVisible();
});
