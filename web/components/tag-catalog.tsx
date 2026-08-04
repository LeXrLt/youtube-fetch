"use client";

import { Hash, Search, X } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import type { TagSummary } from "@/lib/types";

export function TagCatalog({ tags }: Readonly<{ tags: TagSummary[] }>) {
  const [query, setQuery] = useState("");
  const filteredTags = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    if (!normalized) return tags;
    return tags.filter((tag) =>
      [tag.name, tag.category, tag.description]
        .filter(Boolean)
        .some((value) => value!.toLocaleLowerCase("zh-CN").includes(normalized)),
    );
  }, [query, tags]);

  return (
    <>
      <div className="tag-filter">
        <div className="feed-search">
          <Search size={17} aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="筛选标签"
            placeholder="筛选标签"
            maxLength={120}
          />
          {query ? (
            <span className="search-actions">
              <button
                className="icon-button"
                type="button"
                onClick={() => setQuery("")}
                aria-label="清除筛选"
                title="清除筛选"
              >
                <X size={16} />
              </button>
            </span>
          ) : null}
        </div>
      </div>

      {filteredTags.length ? (
        <div className="tags-grid">
          {filteredTags.map((tag) => (
            <Link className="tag-card" href={`/tags/${tag.id}`} key={tag.id}>
              <span className="tag-card-name">
                <Hash size={17} aria-hidden="true" />
                <span>{tag.name}</span>
              </span>
              {tag.category ? <span className="tag-category">{tag.category}</span> : null}
              {tag.description ? (
                <span className="tag-card-description">{tag.description}</span>
              ) : null}
              <span className="tag-card-count">
                {tag.postCount.toLocaleString("zh-CN")} 条字幕
              </span>
            </Link>
          ))}
        </div>
      ) : (
        <EmptyState title="没有匹配的标签" />
      )}
    </>
  );
}
