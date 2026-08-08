"use client";

import { Check, ExternalLink, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useActionState, useEffect, useRef, useState, useTransition } from "react";

import {
  addChannelAction,
  setChannelActiveAction,
  type AddChannelState,
} from "@/app/channels/actions";
import { Avatar } from "@/components/avatar";
import { EmptyState } from "@/components/empty-state";
import { safeHttpUrl } from "@/lib/public-url";
import type { ManagedChannel } from "@/lib/types";

const INITIAL_ADD_STATE: AddChannelState = { status: "idle", message: "" };

export function ChannelManager({ channels }: Readonly<{ channels: ManagedChannel[] }>) {
  const formRef = useRef<HTMLFormElement>(null);
  const [addState, formAction, addPending] = useActionState(
    addChannelAction,
    INITIAL_ADD_STATE,
  );
  const [statusError, setStatusError] = useState("");

  useEffect(() => {
    if (addState.status === "success") {
      formRef.current?.reset();
    }
  }, [addState]);

  return (
    <>
      <form ref={formRef} className="channel-add-form" action={formAction}>
        <div className="channel-add-control">
          <input
            type="text"
            name="channel"
            aria-label="频道链接或用户 ID"
            aria-describedby={addState.message ? "channel-add-message" : undefined}
            aria-invalid={addState.status === "error"}
            placeholder="频道链接或用户 ID，例如 @OpenAI"
            maxLength={512}
            autoComplete="off"
            disabled={addPending}
            required
          />
          <button className="channel-add-button" type="submit" disabled={addPending}>
            {addPending ? (
              <LoaderCircle className="spin" size={17} aria-hidden="true" />
            ) : (
              <Check size={17} aria-hidden="true" />
            )}
            <span>{addPending ? "验证中" : "确认"}</span>
          </button>
        </div>
        {addState.message ? (
          <p
            className={`channel-form-message is-${addState.status}`}
            id="channel-add-message"
            role={addState.status === "error" ? "alert" : "status"}
          >
            {addState.message}
          </p>
        ) : null}
      </form>

      {statusError ? (
        <p className="channel-status-error" role="alert">
          {statusError}
        </p>
      ) : null}

      {channels.length ? (
        <ul className="channel-management-list">
          {channels.map((channel) => (
            <ChannelRow
              channel={channel}
              key={channel.id}
              onStatusError={setStatusError}
            />
          ))}
        </ul>
      ) : (
        <EmptyState title="暂无频道" />
      )}
    </>
  );
}

function ChannelRow({
  channel,
  onStatusError,
}: Readonly<{
  channel: ManagedChannel;
  onStatusError: (message: string) => void;
}>) {
  const [pending, startTransition] = useTransition();
  const channelUrl = safeHttpUrl(channel.url);

  function toggleActive() {
    onStatusError("");
    startTransition(async () => {
      try {
        const result = await setChannelActiveAction(channel.id, !channel.isActive);
        if (!result.ok) {
          onStatusError(result.message);
        }
      } catch {
        onStatusError("频道状态更新失败。");
      }
    });
  }

  return (
    <li className={`channel-management-row${channel.isActive ? "" : " is-inactive"}`}>
      <Avatar src={channel.avatarUrl} name={channel.title} size={48} />
      <div className="channel-management-copy">
        <div className="channel-management-byline">
          <Link className="channel-management-name" href={`/channels/${channel.id}`}>
            {channel.title}
          </Link>
          {channel.handle ? (
            <span className="channel-management-handle">{channel.handle}</span>
          ) : null}
        </div>
        <p className="channel-management-id">{channel.youtubeChannelId}</p>
        {channel.description ? (
          <p className="channel-management-description">{channel.description}</p>
        ) : null}
        <p className="channel-management-count">
          {channel.postCount.toLocaleString("zh-CN")} 条中文字幕
        </p>
      </div>
      <div className="channel-management-actions">
        {channelUrl ? (
          <a
            className="channel-external-icon"
            href={channelUrl}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`在 YouTube 查看 ${channel.title}`}
            title="在 YouTube 查看"
          >
            <ExternalLink size={17} />
          </a>
        ) : null}
        <button
          className="channel-active-switch"
          type="button"
          role="switch"
          aria-checked={channel.isActive}
          aria-label={`${channel.isActive ? "停用" : "启用"} ${channel.title}`}
          title={channel.isActive ? "停用频道" : "启用频道"}
          disabled={pending}
          onClick={toggleActive}
        >
          <span className="channel-switch-track" aria-hidden="true">
            <span className="channel-switch-thumb">
              {pending ? <LoaderCircle className="spin" size={12} /> : null}
            </span>
          </span>
          <span className="channel-switch-label">
            {channel.isActive ? "已启用" : "未启用"}
          </span>
        </button>
      </div>
    </li>
  );
}
