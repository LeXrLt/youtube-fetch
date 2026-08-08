import {
  PAGE_SIZE,
  buildLikePattern,
  paginationOffset,
  type ParsedFeedQuery,
} from "./query-params";
import type { FeedMode, ResolvedChannel } from "./types";

export interface SqlQuery {
  text: string;
  values: unknown[];
}

export interface PagedSqlQueries {
  count: SqlQuery;
  rows: SqlQuery;
}

interface FeedQueryOptions extends ParsedFeedQuery {
  channelId?: string;
  mode: FeedMode;
}

const LATEST_SUBTITLES_CTE = `
latest_subtitles AS (
  SELECT DISTINCT ON (subtitle.video_id)
    subtitle.id,
    subtitle.video_id,
    subtitle.language_code,
    subtitle.language_name,
    subtitle.is_auto_generated,
    subtitle.raw_text,
    subtitle.normalized_text,
    subtitle.translated_text,
    subtitle.translated_language_code
  FROM subtitle_tracks AS subtitle
  ORDER BY
    subtitle.video_id,
    subtitle.fetched_at DESC,
    subtitle.created_at DESC,
    subtitle.id DESC
)`;

const CURRENT_ANALYSES_CTE = `
current_analyses AS (
  SELECT DISTINCT ON (analysis.video_id)
    analysis.id,
    analysis.video_id,
    analysis.subtitle_track_id
  FROM video_analyses AS analysis
  INNER JOIN analysis_runs AS analysis_run
    ON analysis_run.id = analysis.analysis_run_id
   AND analysis_run.status = 'succeeded'
  WHERE analysis.subtitle_track_id IS NOT NULL
  ORDER BY
    analysis.video_id,
    analysis.analyzed_at DESC,
    analysis.created_at DESC,
    analysis.id DESC
)`;

const DISPLAYABLE_CHINESE_TRANSLATION = `(
  (
    lower(subtitle.translated_language_code) = 'zh'
    OR lower(subtitle.translated_language_code) LIKE 'zh-%'
  )
  AND NULLIF(btrim(subtitle.translated_text), '') IS NOT NULL
)`;

const POST_TAGS_LATERAL = `
LEFT JOIN LATERAL (
  SELECT jsonb_agg(
    jsonb_build_object(
      'id', tag.id,
      'name', tag.name,
      'category', tag.category
    )
    ORDER BY tag.name, tag.id
  ) AS tags
  FROM video_analysis_tags AS association
  INNER JOIN tags AS tag ON tag.id = association.tag_id
  WHERE association.video_analysis_id = latest_analysis.id
) AS post_tags ON true`;

const FEED_COLUMNS = `
  post.video_id,
  post.subtitle_id,
  post.youtube_video_id,
  post.video_title,
  post.video_url,
  post.video_description,
  post.duration_seconds,
  post.published_at,
  post.transcript,
  post.language_code,
  post.language_name,
  post.is_auto_generated,
  post.channel_id,
  post.channel_title,
  post.channel_handle,
  post.channel_url,
  post.channel_description,
  post.channel_avatar_url,
  COALESCE(post_tags.tags, '[]'::jsonb) AS tags`;

function addValue(values: unknown[], value: unknown): string {
  values.push(value);
  return `$${values.length}`;
}

function searchCondition(patternPlaceholder: string, transcriptExpression: string): string {
  return `(
    video.title ILIKE ${patternPlaceholder} ESCAPE '\\'
    OR channel.title ILIKE ${patternPlaceholder} ESCAPE '\\'
    OR COALESCE(channel.handle, '') ILIKE ${patternPlaceholder} ESCAPE '\\'
    OR ${transcriptExpression} ILIKE ${patternPlaceholder} ESCAPE '\\'
  )`;
}

export function buildFeedQueries(options: FeedQueryOptions): PagedSqlQueries {
  const filterValues: unknown[] = [];
  const transcriptExpression =
    options.mode === "translated"
      ? "subtitle.translated_text"
      : "COALESCE(subtitle.normalized_text, subtitle.raw_text)";
  const conditions: string[] = [];

  if (options.mode === "translated") {
    conditions.push(DISPLAYABLE_CHINESE_TRANSLATION);
  }

  if (options.channelId) {
    const channelPlaceholder = addValue(filterValues, options.channelId);
    conditions.push(`video.channel_id = ${channelPlaceholder}::uuid`);
  }

  if (options.q) {
    const searchPlaceholder = addValue(filterValues, buildLikePattern(options.q));
    conditions.push(searchCondition(searchPlaceholder, transcriptExpression));
  }

  const whereClause = conditions.length > 0 ? `WHERE ${conditions.join("\n    AND ")}` : "";
  const eligiblePostsCte = `
eligible_posts AS (
  SELECT
    video.id AS video_id,
    subtitle.id AS subtitle_id,
    video.youtube_video_id,
    video.title AS video_title,
    video.video_url,
    video.description AS video_description,
    video.duration_seconds,
    video.published_at,
    video.created_at AS video_created_at,
    ${transcriptExpression} AS transcript,
    subtitle.language_code,
    subtitle.language_name,
    subtitle.is_auto_generated,
    channel.id AS channel_id,
    channel.title AS channel_title,
    channel.handle AS channel_handle,
    channel.channel_url,
    channel.description AS channel_description,
    channel.avatar_url AS channel_avatar_url
  FROM videos AS video
  INNER JOIN youtube_channels AS channel ON channel.id = video.channel_id
  INNER JOIN latest_subtitles AS subtitle ON subtitle.video_id = video.id
  ${whereClause}
)`;
  const commonCtes = `WITH ${LATEST_SUBTITLES_CTE},${eligiblePostsCte}`;
  const rowValues = [...filterValues];
  const limitPlaceholder = addValue(rowValues, PAGE_SIZE);
  const offsetPlaceholder = addValue(rowValues, paginationOffset(options.page));

  return {
    count: {
      text: `${commonCtes}
SELECT count(*) AS total_count
FROM eligible_posts`,
      values: filterValues,
    },
    rows: {
      text: `${commonCtes}
SELECT${FEED_COLUMNS}
FROM eligible_posts AS post
LEFT JOIN LATERAL (
  SELECT analysis.id
  FROM video_analyses AS analysis
  INNER JOIN analysis_runs AS analysis_run
    ON analysis_run.id = analysis.analysis_run_id
   AND analysis_run.status = 'succeeded'
  WHERE analysis.video_id = post.video_id
    AND analysis.subtitle_track_id = post.subtitle_id
  ORDER BY
    analysis.analyzed_at DESC,
    analysis.created_at DESC,
    analysis.id DESC
  LIMIT 1
) AS latest_analysis ON true
${POST_TAGS_LATERAL}
ORDER BY
  post.published_at DESC NULLS LAST,
  post.video_created_at DESC,
  post.video_id DESC
LIMIT ${limitPlaceholder}
OFFSET ${offsetPlaceholder}`,
      values: rowValues,
    },
  };
}

export function buildPostDetailQuery(postId: string): SqlQuery {
  return {
    text: `SELECT
  video.id AS video_id,
  subtitle.id AS subtitle_id,
  video.youtube_video_id,
  video.title AS video_title,
  video.video_url,
  video.description AS video_description,
  video.duration_seconds,
  video.published_at,
  COALESCE(subtitle.normalized_text, subtitle.raw_text) AS transcript,
  subtitle.language_code,
  subtitle.language_name,
  subtitle.is_auto_generated,
  channel.id AS channel_id,
  channel.title AS channel_title,
  channel.handle AS channel_handle,
  channel.channel_url,
  channel.description AS channel_description,
  channel.avatar_url AS channel_avatar_url,
  COALESCE(post_tags.tags, '[]'::jsonb) AS tags,
  subtitle.translated_text AS translated_transcript,
  subtitle.translated_language_code,
  latest_analysis.id AS analysis_id,
  latest_analysis.is_relevant AS analysis_is_relevant,
  latest_analysis.relevance_score,
  latest_analysis.quality_score,
  latest_analysis.summary,
  latest_analysis.translated_summary,
  latest_analysis.background_notes,
  COALESCE(latest_analysis.key_points, '[]'::jsonb) AS analysis_key_points,
  CASE
    WHEN latest_analysis.profile_name = 'default'
     AND latest_analysis.output_schema_version = '1'
    THEN latest_analysis.raw_agent_output ->> 'filter_reason'
  END AS filter_reason,
  CASE
    WHEN latest_analysis.profile_name = 'default'
     AND latest_analysis.output_schema_version = '1'
    THEN latest_analysis.raw_agent_output -> 'sources'
  END AS analysis_sources,
  latest_analysis.analyzed_at
FROM subtitle_tracks AS subtitle
INNER JOIN videos AS video ON video.id = subtitle.video_id
INNER JOIN youtube_channels AS channel ON channel.id = video.channel_id
LEFT JOIN LATERAL (
  SELECT
    analysis.id,
    analysis.is_relevant,
    analysis.relevance_score,
    analysis.quality_score,
    analysis.summary,
    analysis.translated_summary,
    analysis.background_notes,
    analysis.key_points,
    analysis.raw_agent_output,
    analysis.profile_name,
    analysis.output_schema_version,
    analysis.analyzed_at
  FROM video_analyses AS analysis
  INNER JOIN analysis_runs AS analysis_run
    ON analysis_run.id = analysis.analysis_run_id
   AND analysis_run.status = 'succeeded'
  WHERE analysis.video_id = video.id
    AND analysis.subtitle_track_id = subtitle.id
  ORDER BY
    analysis.analyzed_at DESC,
    analysis.created_at DESC,
    analysis.id DESC
  LIMIT 1
) AS latest_analysis ON true
${POST_TAGS_LATERAL}
WHERE subtitle.id = $1::uuid
LIMIT 1`,
    values: [postId],
  };
}

export function buildTagFeedQueries(
  tagId: string,
  query: ParsedFeedQuery,
): PagedSqlQueries {
  const filterValues: unknown[] = [];
  const tagPlaceholder = addValue(filterValues, tagId);
  const transcriptExpression = "COALESCE(subtitle.normalized_text, subtitle.raw_text)";
  const conditions = [`association.tag_id = ${tagPlaceholder}::uuid`];

  if (query.q) {
    const searchPlaceholder = addValue(filterValues, buildLikePattern(query.q));
    conditions.push(searchCondition(searchPlaceholder, transcriptExpression));
  }

  const taggedPostsCte = `
tagged_posts AS (
  SELECT
    video.id AS video_id,
    subtitle.id AS subtitle_id,
    video.youtube_video_id,
    video.title AS video_title,
    video.video_url,
    video.description AS video_description,
    video.duration_seconds,
    video.published_at,
    video.created_at AS video_created_at,
    ${transcriptExpression} AS transcript,
    subtitle.language_code,
    subtitle.language_name,
    subtitle.is_auto_generated,
    channel.id AS channel_id,
    channel.title AS channel_title,
    channel.handle AS channel_handle,
    channel.channel_url,
    channel.description AS channel_description,
    channel.avatar_url AS channel_avatar_url,
    analysis.id AS analysis_id
  FROM current_analyses AS analysis
  INNER JOIN video_analysis_tags AS association
    ON association.video_analysis_id = analysis.id
  INNER JOIN subtitle_tracks AS subtitle
    ON subtitle.id = analysis.subtitle_track_id
   AND subtitle.video_id = analysis.video_id
  INNER JOIN videos AS video ON video.id = analysis.video_id
  INNER JOIN youtube_channels AS channel ON channel.id = video.channel_id
  WHERE ${conditions.join("\n    AND ")}
)`;
  const commonCtes = `WITH ${CURRENT_ANALYSES_CTE},${taggedPostsCte}`;
  const rowValues = [...filterValues];
  const limitPlaceholder = addValue(rowValues, PAGE_SIZE);
  const offsetPlaceholder = addValue(rowValues, paginationOffset(query.page));

  return {
    count: {
      text: `${commonCtes}
SELECT count(*) AS total_count
FROM tagged_posts`,
      values: filterValues,
    },
    rows: {
      text: `${commonCtes}
SELECT${FEED_COLUMNS}
FROM tagged_posts AS post
LEFT JOIN LATERAL (
  SELECT post.analysis_id AS id
) AS latest_analysis ON true
${POST_TAGS_LATERAL}
ORDER BY
  post.published_at DESC NULLS LAST,
  post.video_created_at DESC,
  post.video_id DESC
LIMIT ${limitPlaceholder}
OFFSET ${offsetPlaceholder}`,
      values: rowValues,
    },
  };
}

export function buildTagsQuery(): SqlQuery {
  return {
    text: `WITH ${CURRENT_ANALYSES_CTE}
SELECT
  tag.id,
  tag.name,
  tag.category,
  tag.description,
  count(*) AS post_count
FROM current_analyses AS analysis
INNER JOIN video_analysis_tags AS association
  ON association.video_analysis_id = analysis.id
INNER JOIN tags AS tag ON tag.id = association.tag_id
GROUP BY tag.id, tag.name, tag.category, tag.description
ORDER BY post_count DESC, tag.name, tag.id`,
    values: [],
  };
}

export function buildTagSummaryQuery(tagId: string): SqlQuery {
  return {
    text: `WITH ${CURRENT_ANALYSES_CTE}
SELECT
  tag.id,
  tag.name,
  tag.category,
  tag.description,
  count(analysis.id) AS post_count
FROM tags AS tag
LEFT JOIN video_analysis_tags AS association ON association.tag_id = tag.id
LEFT JOIN current_analyses AS analysis ON analysis.id = association.video_analysis_id
WHERE tag.id = $1::uuid
GROUP BY tag.id, tag.name, tag.category, tag.description`,
    values: [tagId],
  };
}

export function buildChannelSummaryQuery(channelId: string): SqlQuery {
  return {
    text: `WITH ${LATEST_SUBTITLES_CTE}
SELECT
  channel.id,
  channel.title,
  channel.handle,
  channel.channel_url,
  channel.description,
  channel.avatar_url,
  count(subtitle.id) FILTER (
    WHERE ${DISPLAYABLE_CHINESE_TRANSLATION}
  ) AS post_count
FROM youtube_channels AS channel
LEFT JOIN videos AS video ON video.channel_id = channel.id
LEFT JOIN latest_subtitles AS subtitle ON subtitle.video_id = video.id
WHERE channel.id = $1::uuid
GROUP BY
  channel.id,
  channel.title,
  channel.handle,
  channel.channel_url,
  channel.description,
  channel.avatar_url`,
    values: [channelId],
  };
}

export function buildSidebarChannelsQuery(limit: number): SqlQuery {
  return {
    text: `WITH ${LATEST_SUBTITLES_CTE}
SELECT
  channel.id,
  channel.title,
  channel.handle,
  channel.channel_url,
  channel.description,
  channel.avatar_url,
  count(subtitle.id) FILTER (
    WHERE ${DISPLAYABLE_CHINESE_TRANSLATION}
  ) AS post_count
FROM youtube_channels AS channel
LEFT JOIN videos AS video ON video.channel_id = channel.id
LEFT JOIN latest_subtitles AS subtitle ON subtitle.video_id = video.id
WHERE channel.is_active = true
GROUP BY
  channel.id,
  channel.title,
  channel.handle,
  channel.channel_url,
  channel.description,
  channel.avatar_url
ORDER BY post_count DESC, channel.title, channel.id
LIMIT $1`,
    values: [limit],
  };
}

export function buildManagedChannelsQuery(): SqlQuery {
  return {
    text: `WITH ${LATEST_SUBTITLES_CTE}
SELECT
  channel.id,
  channel.youtube_channel_id,
  channel.title,
  channel.handle,
  channel.channel_url,
  channel.description,
  channel.avatar_url,
  channel.is_active,
  count(subtitle.id) FILTER (
    WHERE ${DISPLAYABLE_CHINESE_TRANSLATION}
  ) AS post_count
FROM youtube_channels AS channel
LEFT JOIN videos AS video ON video.channel_id = channel.id
LEFT JOIN latest_subtitles AS subtitle ON subtitle.video_id = video.id
GROUP BY
  channel.id,
  channel.youtube_channel_id,
  channel.title,
  channel.handle,
  channel.channel_url,
  channel.description,
  channel.avatar_url,
  channel.is_active
ORDER BY channel.is_active DESC, channel.title, channel.id`,
    values: [],
  };
}

export function buildUpsertManagedChannelQuery(channel: ResolvedChannel): SqlQuery {
  return {
    text: `INSERT INTO youtube_channels(
  youtube_channel_id,
  handle,
  title,
  channel_url,
  description,
  avatar_url,
  is_active
)
VALUES ($1, $2, $3, $4, $5, $6, true)
ON CONFLICT (youtube_channel_id) DO UPDATE
SET handle = EXCLUDED.handle,
    title = EXCLUDED.title,
    channel_url = EXCLUDED.channel_url,
    description = COALESCE(EXCLUDED.description, youtube_channels.description),
    avatar_url = COALESCE(EXCLUDED.avatar_url, youtube_channels.avatar_url),
    is_active = true,
    updated_at = now()
RETURNING id`,
    values: [
      channel.youtubeChannelId,
      channel.handle,
      channel.title,
      channel.url,
      channel.description,
      channel.avatarUrl,
    ],
  };
}

export function buildSetChannelActiveQuery(
  channelId: string,
  isActive: boolean,
): SqlQuery {
  return {
    text: `UPDATE youtube_channels
SET is_active = $2,
    updated_at = now()
WHERE id = $1::uuid
RETURNING id`,
    values: [channelId, isActive],
  };
}

export function buildTopTagsQuery(limit: number): SqlQuery {
  return {
    text: `WITH ${CURRENT_ANALYSES_CTE}
SELECT
  tag.id,
  tag.name,
  tag.category,
  tag.description,
  count(*) AS post_count
FROM current_analyses AS analysis
INNER JOIN video_analysis_tags AS association
  ON association.video_analysis_id = analysis.id
INNER JOIN tags AS tag ON tag.id = association.tag_id
GROUP BY tag.id, tag.name, tag.category, tag.description
ORDER BY post_count DESC, tag.name, tag.id
LIMIT $1`,
    values: [limit],
  };
}
