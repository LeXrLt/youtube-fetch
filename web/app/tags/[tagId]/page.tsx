import { ArrowLeft } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { AppShell } from "@/components/app-shell";
import { DiscoveryRail } from "@/components/discovery-rail";
import { Feed } from "@/components/feed";
import { FeedHeader } from "@/components/feed-header";
import { Pagination } from "@/components/pagination";
import { getSidebarData, getTagDetail } from "@/lib/data";
import { parseFeedQuery } from "@/lib/query-params";

export const metadata: Metadata = {
  title: "标签字幕",
};

export default async function TagDetailPage(props: PageProps<"/tags/[tagId]">) {
  const [{ tagId }, searchParams] = await Promise.all([
    props.params,
    props.searchParams,
  ]);
  const query = parseFeedQuery(searchParams);
  const [detail, sidebar] = await Promise.all([
    getTagDetail(tagId, query),
    getSidebarData(),
  ]);

  if (!detail) notFound();

  const basePath = `/tags/${tagId}`;

  return (
    <AppShell
      rightRail={<DiscoveryRail channels={sidebar.channels} tags={sidebar.topTags} />}
    >
      <header className="tag-detail-header">
        <Link className="back-link" href="/tags">
          <ArrowLeft size={15} aria-hidden="true" />
          全部标签
        </Link>
        <h1 className="tag-detail-title">#{detail.tag.name}</h1>
        {detail.tag.category ? (
          <p className="tag-detail-category">{detail.tag.category}</p>
        ) : null}
        {detail.tag.description ? (
          <p className="tag-detail-description">{detail.tag.description}</p>
        ) : null}
      </header>

      <FeedHeader
        title="原字幕"
        count={detail.posts.totalItems}
        query={query.q}
        searchAction={basePath}
        headingLevel={2}
      />
      <Feed
        posts={detail.posts.items}
        mode="original"
        emptyTitle={query.q ? "没有匹配的原字幕" : "这个标签暂无字幕"}
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
