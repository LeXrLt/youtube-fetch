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

test("opens a full transcript from the 100-character feed preview", async ({ page }) => {
  await page.goto(withWebBasePath("/"));

  await expect(page.getByRole("heading", { name: "首页", level: 1 })).toBeVisible();
  const firstPost = page.locator("article.transcript-post").first();
  await expect(firstPost).toBeVisible();
  await expect(firstPost.getByText("简体中文", { exact: true })).toBeVisible();

  const preview = (await firstPost.locator(".transcript-preview").textContent()) ?? "";
  expect(Array.from(preview).length).toBeLessThanOrEqual(101);
  if (preview.endsWith("…")) {
    expect(Array.from(preview)).toHaveLength(101);
  }
  await expect(firstPost.getByRole("button", { name: "展开全文" })).toHaveCount(0);

  const detailLink = firstPost.locator(".post-detail-link");
  const detailHref = await detailLink.getAttribute("href");
  expect(detailHref).not.toBeNull();
  const detailUrl = new URL(detailHref ?? "", page.url());
  expect(detailUrl.pathname).toMatch(
    new RegExp(
      `^${withWebBasePath("/posts/")}[0-9a-f-]{36}$`,
    ),
  );
  expect(detailUrl.searchParams.get("mode")).toBe("translated");

  await detailLink.click();
  await expect(page).toHaveURL(detailUrl.href);
  await expect(page.getByRole("heading", { name: "字幕全文", level: 2 })).toBeVisible();
  const fullTranscript =
    (await page.locator(".post-detail-transcript").textContent()) ?? "";
  const normalizedFullTranscript = fullTranscript.replace(/\s+/g, " ").trim();
  const previewText = preview.endsWith("…")
    ? Array.from(preview).slice(0, 100).join("")
    : preview;
  expect(normalizedFullTranscript.startsWith(previewText)).toBe(true);
  const analysisRail = page.getByRole("complementary", { name: "AI 分析" });
  await expect(analysisRail).toBeVisible();

  const timelineBox = await page.locator("main.timeline-column").boundingBox();
  const analysisBox = await analysisRail.boundingBox();
  expect(timelineBox).not.toBeNull();
  expect(analysisBox).not.toBeNull();
  const detailTabs = page.getByRole("navigation", { name: "详情内容" });
  if ((page.viewportSize()?.width ?? 0) > 1050) {
    await expect(detailTabs).toBeHidden();
    expect(analysisBox?.x).toBeGreaterThanOrEqual(
      (timelineBox?.x ?? 0) + (timelineBox?.width ?? 0) - 1,
    );
  } else {
    await expect(detailTabs).toBeVisible();
    expect(analysisBox?.y).toBeGreaterThanOrEqual(
      (timelineBox?.y ?? 0) + (timelineBox?.height ?? 0) - 1,
    );
  }

  await page.getByRole("link", { name: "字幕", exact: true }).click();
  await page.waitForURL(
    (url) => url.pathname === withWebBasePath("/subtitles"),
  );
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
  const firstPost = page.locator("article.transcript-post").first();
  await expect(firstPost).toBeVisible();

  const tagLink = firstPost.getByRole("link", { name: `#${firstName}`, exact: true });
  await expect(tagLink).toBeVisible();
  await tagLink.click();
  await expect(page.getByRole("heading", { name: `#${firstName}`, level: 1 })).toBeVisible();

  const detailLink = page
    .locator("article.transcript-post")
    .first()
    .locator(".post-detail-link");
  const detailHref = await detailLink.getAttribute("href");
  expect(detailHref).not.toBeNull();
  const detailUrl = new URL(detailHref ?? "", page.url());
  expect(detailUrl.pathname).toMatch(
    new RegExp(`^${withWebBasePath("/posts/")}[0-9a-f-]{36}$`),
  );
  expect(detailUrl.searchParams.has("mode")).toBe(false);

  await detailLink.click();
  await expect(page).toHaveURL(detailUrl.href);
  await expect(page.getByRole("heading", { name: "字幕全文", level: 2 })).toBeVisible();
  await expect(
    page.locator(".post-detail-tags").getByRole("link", {
      name: `#${firstName}`,
      exact: true,
    }),
  ).toBeVisible();
  const analysis = page.getByRole("complementary", { name: "AI 分析" });
  await expect(analysis.getByRole("heading", { name: "AI 分析", level: 2 })).toBeVisible();
  await expect(analysis.locator(".analysis-status")).toBeVisible();
  await expect(
    analysis.locator(".analysis-tags").getByRole("link", {
      name: `#${firstName}`,
      exact: true,
    }),
  ).toBeVisible();

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
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
