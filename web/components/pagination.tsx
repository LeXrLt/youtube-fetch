import { ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";

function pageHref(basePath: string, page: number, query?: string) {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  if (page > 1) params.set("page", String(page));
  const encoded = params.toString();
  return encoded ? `${basePath}?${encoded}` : basePath;
}

export function Pagination({
  basePath,
  page,
  totalPages,
  query,
}: Readonly<{
  basePath: string;
  page: number;
  totalPages: number;
  query?: string;
}>) {
  if (totalPages <= 1) return null;

  return (
    <nav className="pagination" aria-label="分页">
      {page > 1 ? (
        <Link
          className="page-button"
          href={pageHref(basePath, page - 1, query)}
          aria-label="上一页"
          title="上一页"
        >
          <ChevronLeft size={19} />
        </Link>
      ) : (
        <span className="page-button is-disabled" aria-hidden="true">
          <ChevronLeft size={19} />
        </span>
      )}

      <span className="page-status">
        {page} / {totalPages}
      </span>

      {page < totalPages ? (
        <Link
          className="page-button"
          href={pageHref(basePath, page + 1, query)}
          aria-label="下一页"
          title="下一页"
        >
          <ChevronRight size={19} />
        </Link>
      ) : (
        <span className="page-button is-disabled" aria-hidden="true">
          <ChevronRight size={19} />
        </span>
      )}
    </nav>
  );
}
