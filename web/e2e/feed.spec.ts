import { expect, test } from "@playwright/test";

test("navigates the live feeds and expands a transcript", async ({ page }) => {
  await page.goto("/");

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
  await page.goto("/tags");

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
  await page.goto("/");

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
