import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";
import { ChannelManager } from "@/components/channel-manager";
import { DiscoveryRail } from "@/components/discovery-rail";
import { FeedHeader } from "@/components/feed-header";
import { getManagedChannels, getSidebarData } from "@/lib/data";

export const metadata: Metadata = {
  title: "频道",
};

export const maxDuration = 90;

export default async function ChannelsPage() {
  const [channels, sidebar] = await Promise.all([
    getManagedChannels(),
    getSidebarData(),
  ]);

  return (
    <AppShell
      rightRail={<DiscoveryRail channels={sidebar.channels} tags={sidebar.topTags} />}
    >
      <FeedHeader
        title="频道"
        count={channels.length}
        countUnit="个"
        searchAction="/channels"
        searchable={false}
      />
      <ChannelManager channels={channels} />
    </AppShell>
  );
}
