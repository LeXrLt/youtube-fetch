import { SiteNav } from "@/components/site-nav";

export function AppShell({
  children,
  rightRail,
}: Readonly<{
  children: React.ReactNode;
  rightRail?: React.ReactNode;
}>) {
  return (
    <div className="app-shell">
      <SiteNav />
      <main className="timeline-column">{children}</main>
      <aside className="right-rail" aria-label="发现">
        {rightRail}
      </aside>
    </div>
  );
}
