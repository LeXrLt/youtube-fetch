import "server-only";

import type { QueryResultRow } from "pg";

import { getChannelManagementPool } from "./pool";
import { isUuid } from "./query-params";
import {
  buildSetChannelActiveQuery,
  buildUpsertManagedChannelQuery,
} from "./queries";
import type { ResolvedChannel } from "./types";

interface IdRow extends QueryResultRow {
  id: string;
}

export class ChannelManagementError extends Error {}

export async function upsertManagedChannel(
  channel: ResolvedChannel,
): Promise<string> {
  const query = buildUpsertManagedChannelQuery(channel);
  const result = await getChannelManagementPool().query<IdRow>(query.text, query.values);
  const channelId = result.rows[0]?.id;
  if (!channelId) {
    throw new ChannelManagementError("Channel upsert did not return an id");
  }
  return channelId;
}

export async function setManagedChannelActive(
  channelId: string,
  isActive: boolean,
): Promise<void> {
  if (!isUuid(channelId)) {
    throw new ChannelManagementError("Invalid channel id");
  }
  const query = buildSetChannelActiveQuery(channelId, isActive);
  const result = await getChannelManagementPool().query<IdRow>(query.text, query.values);
  if (!result.rows[0]) {
    throw new ChannelManagementError("Channel does not exist");
  }
}
