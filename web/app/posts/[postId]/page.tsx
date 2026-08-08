import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { AnalysisPanel } from "@/components/analysis-panel";
import { AppShell } from "@/components/app-shell";
import { PostDetail } from "@/components/post-detail";
import { getPostDetail } from "@/lib/data";
import { parsePostMode } from "@/lib/query-params";

export const metadata: Metadata = {
  title: "帖子详情",
};

export default async function PostDetailPage(props: PageProps<"/posts/[postId]">) {
  const [{ postId }, searchParams] = await Promise.all([
    props.params,
    props.searchParams,
  ]);
  const detail = await getPostDetail(postId);
  if (!detail) notFound();

  const requestedMode = parsePostMode(searchParams.mode);
  const mode =
    requestedMode === "translated" && detail.translatedTranscript
      ? "translated"
      : "original";

  return (
    <AppShell
      variant="post-detail"
      rightRailLabel="AI 分析"
      rightRail={<AnalysisPanel analysis={detail.analysis} tags={detail.post.tags} />}
    >
      <PostDetail detail={detail} mode={mode} />
    </AppShell>
  );
}
