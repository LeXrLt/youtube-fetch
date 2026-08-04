"use client";

import { ChevronDown, ChevronUp } from "lucide-react";
import { useId, useState } from "react";

const COLLAPSE_THRESHOLD = 620;

export function TranscriptBody({ text }: Readonly<{ text: string }>) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const canCollapse = text.length > COLLAPSE_THRESHOLD;

  return (
    <div className="transcript-text">
      <p
        className={`transcript-copy${canCollapse && !expanded ? " is-collapsed" : ""}`}
        id={contentId}
      >
        {text}
      </p>
      {canCollapse ? (
        <button
          className="transcript-toggle"
          type="button"
          aria-controls={contentId}
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? (
            <>
              收起 <ChevronUp size={16} aria-hidden="true" />
            </>
          ) : (
            <>
              展开全文 <ChevronDown size={16} aria-hidden="true" />
            </>
          )}
        </button>
      ) : null}
    </div>
  );
}
