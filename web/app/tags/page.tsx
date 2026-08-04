import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";
import { DiscoveryRail } from "@/components/discovery-rail";
import { EmptyState } from "@/components/empty-state";
import { FeedHeader } from "@/components/feed-header";
import { TagCatalog } from "@/components/tag-catalog";
import { getSidebarData, getTags } from "@/lib/data";

export const metadata: Metadata = {
  title: "标签",
};

export default async function TagsPage() {
  const [tags, sidebar] = await Promise.all([getTags(), getSidebarData()]);

  return (
    <AppShell
      rightRail={<DiscoveryRail channels={sidebar.channels} tags={sidebar.topTags} />}
    >
      <FeedHeader
        title="标签"
        count={tags.length}
        countUnit="个"
        searchAction="/tags"
        searchable={false}
      />
      {tags.length ? <TagCatalog tags={tags} /> : <EmptyState title="暂无标签" />}
    </AppShell>
  );
}
