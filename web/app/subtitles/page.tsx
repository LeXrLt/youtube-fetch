import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";
import { DiscoveryRail } from "@/components/discovery-rail";
import { Feed } from "@/components/feed";
import { FeedHeader } from "@/components/feed-header";
import { Pagination } from "@/components/pagination";
import { getOriginalFeed, getSidebarData } from "@/lib/data";
import { parseFeedQuery } from "@/lib/query-params";

export const metadata: Metadata = {
  title: "字幕",
};

export default async function SubtitlesPage(props: PageProps<"/subtitles">) {
  const searchParams = await props.searchParams;
  const query = parseFeedQuery(searchParams);
  const [feed, sidebar] = await Promise.all([
    getOriginalFeed(query),
    getSidebarData(),
  ]);

  return (
    <AppShell
      rightRail={<DiscoveryRail channels={sidebar.channels} tags={sidebar.topTags} />}
    >
      <FeedHeader
        title="字幕"
        count={feed.totalItems}
        query={query.q}
        searchAction="/subtitles"
      />
      <Feed
        posts={feed.items}
        mode="original"
        emptyTitle={query.q ? "没有匹配的原字幕" : "暂无原字幕"}
        emptyDetail={query.q ? `未找到与“${query.q}”相关的内容` : undefined}
      />
      <Pagination
        basePath="/subtitles"
        page={feed.page}
        totalPages={feed.totalPages}
        query={query.q}
      />
    </AppShell>
  );
}
