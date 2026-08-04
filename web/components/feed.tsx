import { EmptyState } from "@/components/empty-state";
import { TranscriptPost, type TranscriptPostView } from "@/components/transcript-post";

export function Feed({
  posts,
  mode,
  emptyTitle,
  emptyDetail,
}: Readonly<{
  posts: TranscriptPostView[];
  mode: "translated" | "original";
  emptyTitle: string;
  emptyDetail?: string;
}>) {
  if (!posts.length) return <EmptyState title={emptyTitle} detail={emptyDetail} />;

  return (
    <ol className="feed-list">
      {posts.map((post) => (
        <li key={post.subtitleId}>
          <TranscriptPost post={post} mode={mode} />
        </li>
      ))}
    </ol>
  );
}
