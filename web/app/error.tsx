"use client";

import { RefreshCw } from "lucide-react";
import { useEffect } from "react";

export default function ErrorPage({
  error,
  reset,
}: Readonly<{ error: Error & { digest?: string }; reset: () => void }>) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="error-state">
      <h1>暂时无法读取字幕</h1>
      <p>请确认 PostgreSQL 正在运行且数据库配置可用。</p>
      <button className="error-action" type="button" onClick={reset}>
        <RefreshCw size={16} aria-hidden="true" />
        重试
      </button>
    </main>
  );
}
