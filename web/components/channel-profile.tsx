import { ExternalLink } from "lucide-react";

import { Avatar } from "@/components/avatar";
import { safeHttpUrl } from "@/lib/public-url";
import type { ChannelSummary } from "@/lib/types";

export function ChannelProfile({ channel }: Readonly<{ channel: ChannelSummary }>) {
  const channelUrl = safeHttpUrl(channel.url);

  return (
    <header className="channel-profile">
      <div className="channel-profile-main">
        <Avatar src={channel.avatarUrl} name={channel.title} size={74} priority />
        <div className="channel-profile-copy">
          <h1 className="channel-profile-name">{channel.title}</h1>
          {channel.handle ? <div className="channel-profile-handle">{channel.handle}</div> : null}
        </div>
      </div>

      {channel.description ? (
        <p className="channel-description">{channel.description}</p>
      ) : null}

      <div className="channel-stats">
        <span className="channel-stat">
          <strong>{channel.postCount.toLocaleString("zh-CN")}</strong> 条字幕
        </span>
      </div>

      {channelUrl ? (
        <a
          className="channel-external-link"
          href={channelUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          YouTube 频道 <ExternalLink size={14} aria-hidden="true" />
        </a>
      ) : null}
    </header>
  );
}
