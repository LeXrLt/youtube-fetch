import { Bot, ExternalLink } from "lucide-react";
import Link from "next/link";

import { formatPostDate } from "@/lib/post-format";
import { safeHttpUrl } from "@/lib/public-url";
import type { FeedTag, PostAnalysis } from "@/lib/types";

function AnalysisScore({
  label,
  value,
}: Readonly<{ label: string; value: number | null }>) {
  return (
    <div className={`analysis-score${value === null ? " is-empty" : ""}`}>
      <div className="analysis-score-label">
        <span>{label}</span>
        <strong>{value === null ? "--" : Math.round(value)}</strong>
      </div>
      <progress
        max={100}
        value={value ?? undefined}
        aria-label={label}
        aria-valuetext={value === null ? "暂无评分" : `${Math.round(value)} 分`}
      />
    </div>
  );
}

export function AnalysisPanel({
  analysis,
  tags,
}: Readonly<{ analysis: PostAnalysis | null; tags: FeedTag[] }>) {
  if (!analysis) {
    return (
      <div className="analysis-panel" id="ai-analysis">
        <header className="analysis-header">
          <Bot size={19} aria-hidden="true" />
          <h2>AI 分析</h2>
        </header>
        <div className="analysis-empty">
          <p>暂无 AI 分析</p>
        </div>
      </div>
    );
  }

  return (
    <div className="analysis-panel" id="ai-analysis">
      <header className="analysis-header">
        <div className="analysis-title-row">
          <Bot size={19} aria-hidden="true" />
          <h2>AI 分析</h2>
          <span
            className={`analysis-status${analysis.isRelevant ? " is-relevant" : " is-filtered"}`}
          >
            {analysis.isRelevant ? "值得关注" : "低相关"}
          </span>
        </div>
        <time dateTime={analysis.analyzedAt}>
          {formatPostDate(analysis.analyzedAt, "")}
        </time>
      </header>

      <div className="analysis-scores" aria-label="分析评分">
        <AnalysisScore label="相关性" value={analysis.relevanceScore} />
        <AnalysisScore label="内容质量" value={analysis.qualityScore} />
      </div>

      {analysis.summary ? (
        <section className="analysis-section" aria-labelledby="analysis-summary-heading">
          <h3 id="analysis-summary-heading">摘要</h3>
          <p>{analysis.summary}</p>
        </section>
      ) : null}

      {analysis.keyPoints.length ? (
        <section className="analysis-section" aria-labelledby="analysis-points-heading">
          <h3 id="analysis-points-heading">关键要点</h3>
          <ol className="analysis-points">
            {analysis.keyPoints.map((point, index) => (
              <li key={`${index}-${point}`}>{point}</li>
            ))}
          </ol>
        </section>
      ) : null}

      {analysis.backgroundNotes ? (
        <section className="analysis-section" aria-labelledby="analysis-background-heading">
          <h3 id="analysis-background-heading">背景补充</h3>
          <p>{analysis.backgroundNotes}</p>
        </section>
      ) : null}

      {analysis.filterReason ? (
        <section className="analysis-section" aria-labelledby="analysis-filter-heading">
          <h3 id="analysis-filter-heading">筛选说明</h3>
          <p>{analysis.filterReason}</p>
        </section>
      ) : null}

      {tags.length ? (
        <section className="analysis-section" aria-labelledby="analysis-tags-heading">
          <h3 id="analysis-tags-heading">标签</h3>
          <div className="analysis-tags">
            {tags.map((tag) => (
              <Link className="tag-chip" href={`/tags/${tag.id}`} key={tag.id}>
                #{tag.name}
              </Link>
            ))}
          </div>
        </section>
      ) : null}

      {analysis.sources.length ? (
        <section className="analysis-section" aria-labelledby="analysis-sources-heading">
          <h3 id="analysis-sources-heading">参考来源</h3>
          <ul className="analysis-sources">
            {analysis.sources.map((source, index) => {
              const sourceUrl = safeHttpUrl(source.url);
              return (
                <li key={`${index}-${source.title}`}>
                  {sourceUrl ? (
                    <a
                      href={sourceUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {source.title}
                      <ExternalLink size={13} aria-hidden="true" />
                    </a>
                  ) : (
                    <span>{source.title}</span>
                  )}
                  {source.note ? <p>{source.note}</p> : null}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
