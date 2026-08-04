"use client";

import { Captions, House, Tags } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

const navItems = [
  { href: "/", label: "首页", icon: House, matches: (path: string) => path === "/" },
  {
    href: "/subtitles",
    label: "字幕",
    icon: Captions,
    matches: (path: string) => path.startsWith("/subtitles"),
  },
  {
    href: "/tags",
    label: "标签",
    icon: Tags,
    matches: (path: string) => path.startsWith("/tags"),
  },
] as const;

export function SiteNav() {
  const pathname = usePathname();

  return (
    <aside className="site-nav">
      <Link className="brand" href="/" aria-label="字幕流首页">
        <span className="brand-mark" aria-hidden="true">
          <Captions size={23} strokeWidth={2.2} />
        </span>
        <span className="brand-name">字幕流</span>
      </Link>

      <nav className="primary-nav" aria-label="主导航">
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = item.matches(pathname);
          return (
            <Link
              className="nav-link"
              href={item.href}
              key={item.href}
              aria-current={active ? "page" : undefined}
              title={item.label}
            >
              <Icon size={22} strokeWidth={active ? 2.35 : 1.9} />
              <span className="nav-label">{item.label}</span>
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
