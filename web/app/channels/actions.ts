"use server";

import { revalidatePath } from "next/cache";

import { parseChannelReference } from "@/lib/channel-input";
import {
  setManagedChannelActive,
  upsertManagedChannel,
} from "@/lib/channel-management";
import {
  ChannelInspectionError,
  ChannelNotFoundError,
  resolveYoutubeChannel,
} from "@/lib/channel-resolver";

export type AddChannelState = {
  status: "idle" | "error" | "success";
  message: string;
};

export type ChannelStatusResult = {
  ok: boolean;
  message: string;
};

export async function addChannelAction(
  _previousState: AddChannelState,
  formData: FormData,
): Promise<AddChannelState> {
  const reference = parseChannelReference(formData.get("channel"));
  if (!reference) {
    return { status: "error", message: "频道不存在！" };
  }

  let channel;
  try {
    channel = await resolveYoutubeChannel(reference);
  } catch (error) {
    if (error instanceof ChannelNotFoundError) {
      return { status: "error", message: "频道不存在！" };
    }
    if (error instanceof ChannelInspectionError) {
      console.error("Channel inspection failed", error);
      return { status: "error", message: "频道验证失败，请稍后重试。" };
    }
    throw error;
  }

  try {
    await upsertManagedChannel(channel);
  } catch (error) {
    console.error("Channel upsert failed", error);
    return { status: "error", message: "频道保存失败，请稍后重试。" };
  }

  revalidatePath("/channels");
  return { status: "success", message: `已添加 ${channel.title}` };
}

export async function setChannelActiveAction(
  channelId: string,
  isActive: boolean,
): Promise<ChannelStatusResult> {
  if (typeof channelId !== "string" || typeof isActive !== "boolean") {
    return { ok: false, message: "频道状态更新失败。" };
  }

  try {
    await setManagedChannelActive(channelId, isActive);
  } catch (error) {
    console.error("Channel status update failed", error);
    return { ok: false, message: "频道状态更新失败。" };
  }

  revalidatePath("/channels");
  return { ok: true, message: "" };
}
