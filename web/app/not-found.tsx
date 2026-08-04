import { ArrowLeft } from "lucide-react";
import Link from "next/link";

import { AppShell } from "@/components/app-shell";

export default function NotFound() {
  return (
    <AppShell>
      <div className="error-state">
        <h1>没有找到这个页面</h1>
        <Link className="error-action" href="/">
          <ArrowLeft size={16} aria-hidden="true" />
          返回首页
        </Link>
      </div>
    </AppShell>
  );
}
