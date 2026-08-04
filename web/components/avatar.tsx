"use client";

import { useState, type CSSProperties } from "react";

import { safeHttpUrl } from "@/lib/public-url";

export function Avatar({
  src,
  name,
  size = 46,
  priority = false,
}: Readonly<{
  src: string | null;
  name: string;
  size?: number;
  priority?: boolean;
}>) {
  const [failed, setFailed] = useState(false);
  const initial = Array.from(name.trim())[0]?.toLocaleUpperCase() ?? "?";
  const style = { "--avatar-size": `${size}px` } as CSSProperties;
  const imageUrl = safeHttpUrl(src);

  return (
    <span className="avatar" style={style} aria-hidden="true">
      {imageUrl && !failed ? (
        // Channel avatar hosts are data-driven, so a native image avoids a global host allowlist.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imageUrl}
          alt=""
          width={size}
          height={size}
          loading={priority ? "eager" : "lazy"}
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <span>{initial}</span>
      )}
    </span>
  );
}
