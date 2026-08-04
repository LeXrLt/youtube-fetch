import { Search, X } from "lucide-react";
import Link from "next/link";

export function FeedHeader({
  title,
  count,
  query,
  searchAction,
  description,
  searchable = true,
  countUnit = "条",
  headingLevel = 1,
}: Readonly<{
  title: string;
  count?: number;
  query?: string;
  searchAction: string;
  description?: string;
  searchable?: boolean;
  countUnit?: string;
  headingLevel?: 1 | 2;
}>) {
  const Heading = headingLevel === 1 ? "h1" : "h2";

  return (
    <header className="timeline-header">
      <div className="timeline-title-row">
        <Heading className="timeline-title">{title}</Heading>
        {typeof count === "number" ? (
          <span className="timeline-count">
            {count.toLocaleString("zh-CN")} {countUnit}
          </span>
        ) : null}
      </div>
      {description ? <p className="timeline-description">{description}</p> : null}
      {searchable ? (
        <form className="feed-search" action={searchAction} role="search">
          <Search size={17} aria-hidden="true" />
          <input
            type="search"
            name="q"
            defaultValue={query}
            aria-label="搜索字幕或博主"
            placeholder="搜索字幕或博主"
            maxLength={120}
          />
          <span className="search-actions">
            {query ? (
              <Link
                className="icon-button"
                href={searchAction}
                aria-label="清除搜索"
                title="清除搜索"
              >
                <X size={16} />
              </Link>
            ) : null}
            <button className="icon-button" type="submit" aria-label="搜索" title="搜索">
              <Search size={16} />
            </button>
          </span>
        </form>
      ) : null}
    </header>
  );
}
