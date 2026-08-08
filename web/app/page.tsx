import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";
import { DiscoveryRail } from "@/components/discovery-rail";
import { Feed } from "@/components/feed";
import { FeedHeader } from "@/components/feed-header";
import { Pagination } from "@/components/pagination";
import { getSidebarData, getTranslatedFeed } from "@/lib/data";
import { parseFeedQuery } from "@/lib/query-params";

export const metadata: Metadata = {
  title: "首页",
};

export default async function Home(props: PageProps<"/">) {
  const searchParams = await props.searchParams;
  const query = parseFeedQuery(searchParams);
  const [feed, sidebar] = await Promise.all([
    getTranslatedFeed(query),
    getSidebarData(),
  ]);

  return (
    <AppShell
      rightRail={<DiscoveryRail channels={sidebar.channels} tags={sidebar.topTags} />}
    >
      <FeedHeader
        title="首页"
        count={feed.totalItems}
        query={query.q}
        searchAction="/"
      />
      <Feed
        posts={feed.items}
        mode="translated"
        emptyTitle={query.q ? "没有匹配的中文字幕" : "暂无中文字幕"}
        emptyDetail={query.q ? `未找到与“${query.q}”相关的内容` : undefined}
      />
      <Pagination
        basePath="/"
        page={feed.page}
        totalPages={feed.totalPages}
        query={query.q}
      />
    </AppShell>
  );
}
