import { describe, expect, it } from "vitest";

import { PAGE_SIZE, buildLikePattern } from "./query-params";
import {
  buildFeedQueries,
  buildManagedChannelsQuery,
  buildSetChannelActiveQuery,
  buildTagFeedQueries,
  buildTagSummaryQuery,
  buildTagsQuery,
  buildUpsertManagedChannelQuery,
} from "./queries";

const CHANNEL_ID = "28f5f390-62e7-4f27-a478-75d46c331b77";
const TAG_ID = "9d3dd442-ab51-4dd3-8435-952e69f6cfc2";

describe("buildFeedQueries", () => {
  it("selects the latest zh-CN subtitle and parameterizes search and pagination", () => {
    const attack = "100%_' OR true --";
    const query = buildFeedQueries({ mode: "translated", q: attack, page: 2 });

    expect(query.count.text).toContain("SELECT DISTINCT ON (subtitle.video_id)");
    expect(query.count.text).toContain("subtitle.fetched_at DESC");
    expect(query.count.text).toContain("subtitle.created_at DESC");
    expect(query.count.text).toContain("subtitle.id DESC");
    expect(query.count.text).toContain("subtitle.translated_language_code = 'zh-CN'");
    expect(query.count.text).toContain("NULLIF(btrim(subtitle.translated_text), '') IS NOT NULL");
    expect(query.rows.text).toContain("analysis_run.status = 'succeeded'");
    expect(query.rows.text).toContain("analysis.subtitle_track_id = post.subtitle_id");
    expect(query.rows.text).not.toContain(attack);
    expect(query.count.values).toEqual([buildLikePattern(attack)]);
    expect(query.rows.values).toEqual([buildLikePattern(attack), PAGE_SIZE, PAGE_SIZE]);
  });

  it("uses original text fallback and parameterizes the channel id", () => {
    const query = buildFeedQueries({
      mode: "original",
      channelId: CHANNEL_ID,
      q: "handle",
      page: 3,
    });

    expect(query.count.text).toContain("COALESCE(subtitle.normalized_text, subtitle.raw_text)");
    expect(query.count.text).toContain("video.channel_id = $1::uuid");
    expect(query.count.text).toContain("channel.handle");
    expect(query.count.text).not.toContain(CHANNEL_ID);
    expect(query.count.values).toEqual([CHANNEL_ID, "%handle%"]);
    expect(query.rows.values).toEqual([CHANNEL_ID, "%handle%", PAGE_SIZE, PAGE_SIZE * 2]);
  });
});

describe("tag query builders", () => {
  it("chooses one latest succeeded analysis per video", () => {
    const query = buildTagsQuery();

    expect(query.text).toContain("SELECT DISTINCT ON (analysis.video_id)");
    expect(query.text).toContain("analysis_run.status = 'succeeded'");
    expect(query.text).toContain("analysis.analyzed_at DESC");
    expect(query.values).toEqual([]);
  });

  it("returns the original subtitle corresponding to the current tagged analysis", () => {
    const query = buildTagFeedQueries(TAG_ID, { q: "RAG_100%", page: 2 });

    expect(query.count.text).toContain("association.tag_id = $1::uuid");
    expect(query.count.text).toContain("subtitle.id = analysis.subtitle_track_id");
    expect(query.count.text).toContain("subtitle.video_id = analysis.video_id");
    expect(query.count.text).not.toContain(TAG_ID);
    expect(query.count.values).toEqual([TAG_ID, buildLikePattern("RAG_100%")]);
    expect(query.rows.values).toEqual([
      TAG_ID,
      buildLikePattern("RAG_100%"),
      PAGE_SIZE,
      PAGE_SIZE,
    ]);
  });

  it("parameterizes tag detail identity", () => {
    const query = buildTagSummaryQuery(TAG_ID);

    expect(query.text).toContain("WHERE tag.id = $1::uuid");
    expect(query.text).not.toContain(TAG_ID);
    expect(query.values).toEqual([TAG_ID]);
  });
});

describe("channel management query builders", () => {
  it("lists active channels first without hiding inactive channels", () => {
    const query = buildManagedChannelsQuery();

    expect(query.text).toContain("channel.youtube_channel_id");
    expect(query.text).toContain("channel.is_active");
    expect(query.text).toContain("ORDER BY channel.is_active DESC");
    expect(query.text).not.toContain("WHERE channel.is_active = true");
    expect(query.values).toEqual([]);
  });

  it("parameterizes verified metadata and reactivates an existing channel", () => {
    const channel = {
      youtubeChannelId: "UCaaaaaaaaaaaaaaaaaaaaaa",
      title: "Title ' OR true --",
      url: "https://www.youtube.com/@channel",
      handle: "@channel",
      description: "Description",
      avatarUrl: "https://images.example.test/avatar.jpg",
    };
    const query = buildUpsertManagedChannelQuery(channel);

    expect(query.text).toContain("ON CONFLICT (youtube_channel_id) DO UPDATE");
    expect(query.text).toContain("is_active = true");
    expect(query.text).toContain("updated_at = now()");
    expect(query.text).not.toContain(channel.title);
    expect(query.values).toEqual([
      channel.youtubeChannelId,
      channel.handle,
      channel.title,
      channel.url,
      channel.description,
      channel.avatarUrl,
    ]);
  });

  it("parameterizes channel identity and activation state", () => {
    const query = buildSetChannelActiveQuery(CHANNEL_ID, false);

    expect(query.text).toContain("WHERE id = $1::uuid");
    expect(query.text).toContain("is_active = $2");
    expect(query.text).not.toContain(CHANNEL_ID);
    expect(query.values).toEqual([CHANNEL_ID, false]);
  });
});
