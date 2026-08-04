export function FeedSkeleton({ posts = 4 }: Readonly<{ posts?: number }>) {
  return (
    <div aria-label="正在加载" aria-busy="true">
      <div className="skeleton-header">
        <div className="skeleton-line" />
        <div className="skeleton-block" style={{ height: 40 }} />
      </div>
      {Array.from({ length: posts }, (_, index) => (
        <div className="skeleton-post" key={index}>
          <div className="skeleton-avatar" />
          <div>
            <div className="skeleton-line" />
            <div className="skeleton-line is-wide" />
            <div className="skeleton-block" />
          </div>
        </div>
      ))}
    </div>
  );
}
