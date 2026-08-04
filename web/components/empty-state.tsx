import { Captions } from "lucide-react";

export function EmptyState({
  title,
  detail,
}: Readonly<{ title: string; detail?: string }>) {
  return (
    <div className="empty-state">
      <span className="empty-state-icon" aria-hidden="true">
        <Captions size={25} />
      </span>
      <h2>{title}</h2>
      {detail ? <p>{detail}</p> : null}
    </div>
  );
}
