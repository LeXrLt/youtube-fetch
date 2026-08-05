import { describe, expect, it } from "vitest";

import {
  MAX_CHANNEL_REFERENCE_LENGTH,
  parseChannelInspectionPayload,
  parseChannelReference,
} from "./channel-input";

const CHANNEL_ID = "UCaaaaaaaaaaaaaaaaaaaaaa";

describe("parseChannelReference", () => {
  it.each([
    ["  @OpenAI  ", "@OpenAI"],
    [CHANNEL_ID, CHANNEL_ID],
    ["https://www.youtube.com/@OpenAI", "https://www.youtube.com/@OpenAI"],
  ])("normalizes %j to %j", (input, expected) => {
    expect(parseChannelReference(input)).toBe(expected);
  });

  it.each([null, 42, "", " \n ", "bad\0channel"])("rejects %j", (input) => {
    expect(parseChannelReference(input)).toBeNull();
  });

  it("rejects references beyond the server limit", () => {
    expect(parseChannelReference("a".repeat(MAX_CHANNEL_REFERENCE_LENGTH + 1))).toBeNull();
  });
});

describe("parseChannelInspectionPayload", () => {
  it("maps valid Pipeline metadata", () => {
    expect(
      parseChannelInspectionPayload({
        youtube_channel_id: CHANNEL_ID,
        title: " OpenAI ",
        channel_url: "https://www.youtube.com/@OpenAI",
        handle: "@OpenAI",
        description: "Channel description",
        avatar_url: "https://yt3.googleusercontent.com/avatar",
      }),
    ).toEqual({
      youtubeChannelId: CHANNEL_ID,
      title: "OpenAI",
      url: "https://www.youtube.com/@OpenAI",
      handle: "@OpenAI",
      description: "Channel description",
      avatarUrl: "https://yt3.googleusercontent.com/avatar",
    });
  });

  it("accepts nullable optional profile fields", () => {
    expect(
      parseChannelInspectionPayload({
        youtube_channel_id: CHANNEL_ID,
        title: "OpenAI",
        channel_url: `https://www.youtube.com/channel/${CHANNEL_ID}`,
        handle: null,
        description: null,
        avatar_url: null,
      }),
    ).toMatchObject({ handle: null, description: null, avatarUrl: null });
  });

  it.each([
    {},
    {
      youtube_channel_id: "not-a-channel-id",
      title: "OpenAI",
      channel_url: "https://www.youtube.com/@OpenAI",
    },
    {
      youtube_channel_id: CHANNEL_ID,
      title: "OpenAI",
      channel_url: "https://example.com/@OpenAI",
    },
    {
      youtube_channel_id: CHANNEL_ID,
      title: "OpenAI",
      channel_url: "https://www.youtube.com/@OpenAI",
      avatar_url: "javascript:alert(1)",
    },
  ])("rejects invalid metadata %#", (payload) => {
    expect(parseChannelInspectionPayload(payload)).toBeNull();
  });
});
