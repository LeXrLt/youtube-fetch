import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { AppShell } from "@/components/app-shell";
import { ChannelProfile } from "@/components/channel-profile";
import { DiscoveryRail } from "@/components/discovery-rail";
import { Feed } from "@/components/feed";
import { FeedHeader } from "@/components/feed-header";
import { Pagination } from "@/components/pagination";
import { getChannelDetail, getSidebarData } from "@/lib/data";
import { parseFeedQuery } from "@/lib/query-params";

export const metadata: Metadata = {
  title: "博主字幕",
};

export default async function ChannelDetailPage(
  props: PageProps<"/channels/[channelId]">,
) {
  const [{ channelId }, searchParams] = await Promise.all([
    props.params,
    props.searchParams,
  ]);
  const query = parseFeedQuery(searchParams);
  const [detail, sidebar] = await Promise.all([
    getChannelDetail(channelId, query),
    getSidebarData(),
  ]);

  if (!detail) notFound();

  const basePath = `/channels/${channelId}`;

  return (
    <AppShell
      rightRail={<DiscoveryRail channels={sidebar.channels} tags={sidebar.topTags} />}
    >
      <ChannelProfile channel={detail.channel} />
      <FeedHeader
        title="中文字幕"
        count={detail.posts.totalItems}
        query={query.q}
        searchAction={basePath}
        headingLevel={2}
      />
      <Feed
        posts={detail.posts.items}
        mode="translated"
        emptyTitle={query.q ? "没有匹配的中文字幕" : "这个博主暂无中文字幕"}
      />
      <Pagination
        basePath={basePath}
        page={detail.posts.page}
        totalPages={detail.posts.totalPages}
        query={query.q}
      />
    </AppShell>
  );
}
