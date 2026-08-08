import { Hash } from "lucide-react";
import Link from "next/link";

import { Avatar } from "@/components/avatar";

type RailChannel = {
  id: string;
  title: string;
  handle: string | null;
  url: string;
  description: string | null;
  avatarUrl: string | null;
  postCount: number;
};

type RailTag = {
  id: string;
  name: string;
  category: string | null;
  postCount: number;
};

export function DiscoveryRail({
  channels,
  tags,
}: Readonly<{ channels: RailChannel[]; tags: RailTag[] }>) {
  return (
    <>
      {channels.length ? (
        <section className="rail-section" aria-labelledby="channels-heading">
          <h2 className="rail-heading" id="channels-heading">
            博主
          </h2>
          {channels.map((channel) => (
            <Link className="rail-channel" href={`/channels/${channel.id}`} key={channel.id}>
              <Avatar src={channel.avatarUrl} name={channel.title} size={38} />
              <span className="rail-channel-copy">
                <span className="rail-primary">{channel.title}</span>
                <span className="rail-secondary">
                  {channel.description || channel.handle || `${channel.postCount} 条中文字幕`}
                </span>
              </span>
            </Link>
          ))}
        </section>
      ) : null}

      {tags.length ? (
        <section className="rail-section" aria-labelledby="tags-heading">
          <h2 className="rail-heading" id="tags-heading">
            常见标签
          </h2>
          {tags.map((tag) => (
            <Link className="rail-tag" href={`/tags/${tag.id}`} key={tag.id}>
              <span className="rail-tag-icon" aria-hidden="true">
                <Hash size={17} />
              </span>
              <span className="rail-tag-copy">
                <span className="rail-primary">{tag.name}</span>
                <span className="rail-secondary">
                  {tag.category ? `${tag.category} · ` : ""}
                  {tag.postCount} 条字幕
                </span>
              </span>
            </Link>
          ))}
        </section>
      ) : null}
    </>
  );
}
