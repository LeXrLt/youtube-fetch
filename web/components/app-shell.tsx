import { SiteNav } from "@/components/site-nav";

export function AppShell({
  children,
  rightRail,
  variant = "default",
  rightRailLabel = "发现",
}: Readonly<{
  children: React.ReactNode;
  rightRail?: React.ReactNode;
  variant?: "default" | "post-detail";
  rightRailLabel?: string;
}>) {
  return (
    <div className={`app-shell${variant === "post-detail" ? " is-post-detail" : ""}`}>
      <SiteNav />
      <main className="timeline-column">{children}</main>
      <aside className="right-rail" aria-label={rightRailLabel}>
        {rightRail}
      </aside>
    </div>
  );
}
