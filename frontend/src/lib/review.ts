import type { Interjection, MeetingBundle, TranscriptSegment } from "@/lib/api/types";

export type FollowUpStatus = "outstanding" | "resolved";

export type FollowUpItem = {
  id: string;
  action: string;
  owner: string;
  due: string | null;
  status: FollowUpStatus;
  evidenceSegmentId: string;
  evidenceStartMs: number;
  resolutionSegmentId: string | null;
  resolutionStartMs: number | null;
};

export type FollowUpCounts = {
  total: number;
  outstanding: number;
  resolved: number;
};

export type TopicSummary = {
  label: string;
  mentions: number;
  takeaway: string;
  evidenceSegmentId: string | null;
  evidenceStartMs: number | null;
};

export type SessionReview = {
  primaryTakeaway: {
    text: string;
    evidenceSegmentId: string | null;
    evidenceStartMs: number | null;
  };
  topics: TopicSummary[];
  followUps: FollowUpItem[];
  counts: FollowUpCounts;
};

const COMMITMENT_PATTERN =
  /\b(?:i['’]?ll|i\s+will|we['’]?ll|we\s+will|i\s+can|we\s+can|let\s+me)\s+(.+?)(?:[.!?]|$)/i;
const COMPLETION_PATTERN =
  /\b(?:done|completed|resolved|closed|finished|delivered|circulated|sent|shared|submitted|published)\b/i;
const DUE_PATTERN =
  /\b(?:tonight|tomorrow|today|(?:by|before)\s+(?:eod|end of (?:the )?day|close of business|(?:next|this)\s+(?:week|month|quarter|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}))\b/i;

const TOPICS = [
  {
    label: "Revenue & reporting",
    pattern: /\b(?:revenue|bookings?|sales?|top[- ]line|gross|net revenue|board deck|forecast|margin)\b/i,
  },
  {
    label: "Retention & pricing",
    pattern: /\b(?:churn|retention|renewals?|attrition|pricing|price|repric\w*)\b/i,
  },
  {
    label: "Product & pipeline",
    pattern: /\b(?:product|pipeline|launch|roadmap|feature|release)\b/i,
  },
  {
    label: "Decisions & ownership",
    pattern: /\b(?:align|decid\w*|owner|ownership|action|next step|follow[- ]?up|circulate|revisit)\b/i,
  },
  {
    label: "Customers & market",
    pattern: /\b(?:customer|client|market|account|segment|enterprise|mid-market)\b/i,
  },
  {
    label: "Risk & controls",
    pattern: /\b(?:risk|control|compliance|audit|exposure|policy)\b/i,
  },
] as const;

const STOP_WORDS = new Set([
  "about",
  "after",
  "before",
  "from",
  "into",
  "that",
  "their",
  "there",
  "this",
  "tonight",
  "with",
  "will",
  "would",
  "your",
]);

function finalized(transcript: TranscriptSegment[]) {
  return transcript.filter((segment) => segment.is_final).sort((a, b) => a.start_ms - b.start_ms);
}

function sentenceCase(text: string) {
  const trimmed = text.trim().replace(/[.!?]+$/, "");
  if (!trimmed) return "Follow up";
  return `${trimmed.charAt(0).toUpperCase()}${trimmed.slice(1)}`;
}

function compact(text: string, max = 190) {
  const normalized = text.replace(/\s+/g, " ").trim();
  return normalized.length <= max ? normalized : `${normalized.slice(0, max - 1).trimEnd()}…`;
}

function dueFrom(text: string) {
  const match = text.match(DUE_PATTERN)?.[0];
  return match ? sentenceCase(match) : null;
}

function sameSpeaker(a: TranscriptSegment, b: TranscriptSegment) {
  if (a.person_id && b.person_id) return a.person_id === b.person_id;
  return a.speaker_name.toLowerCase() === b.speaker_name.toLowerCase();
}

function keywords(text: string) {
  return new Set(
    text
      .toLowerCase()
      .replace(/[^a-z0-9-]+/g, " ")
      .split(/\s+/)
      .filter((word) => word.length > 3 && !STOP_WORDS.has(word)),
  );
}

function findResolution(
  commitment: TranscriptSegment,
  action: string,
  laterSegments: TranscriptSegment[],
) {
  const actionWords = keywords(action);
  return (
    laterSegments.find((candidate) => {
      if (!sameSpeaker(commitment, candidate) || !COMPLETION_PATTERN.test(candidate.text)) return false;
      const completionWords = keywords(candidate.text);
      return [...actionWords].some((word) => completionWords.has(word));
    }) ?? null
  );
}

export function deriveFollowUps(transcript: TranscriptSegment[]): FollowUpItem[] {
  const segments = finalized(transcript);

  return segments.flatMap((segment, index) => {
    const match = segment.text.match(COMMITMENT_PATTERN);
    if (!match?.[1]) return [];

    const action = sentenceCase(match[1]);
    const resolution = findResolution(segment, action, segments.slice(index + 1));

    return [
      {
        id: `follow-up-${segment.id}`,
        action,
        owner: segment.speaker_name,
        due: dueFrom(action),
        status: resolution ? "resolved" : "outstanding",
        evidenceSegmentId: segment.id,
        evidenceStartMs: segment.start_ms,
        resolutionSegmentId: resolution?.id ?? null,
        resolutionStartMs: resolution?.start_ms ?? null,
      },
    ];
  });
}

function claimText(claim: Interjection) {
  return [claim.headline, claim.body_md, claim.trigger?.quote].filter(Boolean).join(" ");
}

function claimPriority(claim: Interjection) {
  switch (claim.kind) {
    case "contradiction":
      return 0;
    case "correction":
      return 1;
    case "clarification":
      return 2;
    case "context":
      return 3;
    case "answer":
      return 4;
    default:
      return 5;
  }
}

function topicSummaries(bundle: MeetingBundle): TopicSummary[] {
  const transcript = finalized(bundle.transcript);
  const transcriptById = new Map(transcript.map((segment) => [segment.id, segment]));
  const rankedClaims = [...bundle.interjections].sort((a, b) => claimPriority(a) - claimPriority(b));
  const usedClaims = new Set<string>();

  const scored = TOPICS.map((topic) => ({
    ...topic,
    segments: transcript.filter((segment) => topic.pattern.test(segment.text)),
  }))
    .filter((topic) => topic.segments.length > 0)
    .sort((a, b) => b.segments.length - a.segments.length)
    .slice(0, 3);

  if (scored.length === 0) {
    const fallbackClaim = rankedClaims[0];
    const fallbackSegment = transcript[0];
    return [
      {
        label: "Meeting discussion",
        mentions: transcript.length,
        takeaway: compact(fallbackClaim?.headline ?? fallbackSegment?.text ?? "No finalized discussion was recorded."),
        evidenceSegmentId: fallbackClaim?.trigger?.segment_ids?.[0] ?? fallbackSegment?.id ?? null,
        evidenceStartMs:
          transcriptById.get(fallbackClaim?.trigger?.segment_ids?.[0] ?? "")?.start_ms ??
          fallbackSegment?.start_ms ??
          null,
      },
    ];
  }

  return scored.map((topic) => {
    const claim = rankedClaims.find(
      (candidate) => !usedClaims.has(candidate.id) && topic.pattern.test(claimText(candidate)),
    );
    if (claim) usedClaims.add(claim.id);

    const decisionSegment = [...topic.segments]
      .reverse()
      .find((segment) => COMMITMENT_PATTERN.test(segment.text) || TOPICS[3].pattern.test(segment.text));
    const evidenceId = claim?.trigger?.segment_ids?.[0] ?? decisionSegment?.id ?? topic.segments[0]?.id ?? null;
    const evidence = evidenceId ? transcriptById.get(evidenceId) : null;

    return {
      label: topic.label,
      mentions: topic.segments.length,
      takeaway: compact(claim?.headline ?? decisionSegment?.text ?? topic.segments.at(-1)?.text ?? ""),
      evidenceSegmentId: evidenceId,
      evidenceStartMs: evidence?.start_ms ?? null,
    };
  });
}

export function deriveSessionReview(bundle: MeetingBundle): SessionReview {
  const followUps = deriveFollowUps(bundle.transcript);
  const outstanding = followUps.filter((item) => item.status === "outstanding").length;
  const resolved = followUps.length - outstanding;
  const topics = topicSummaries(bundle);

  const primaryClaim = [...bundle.interjections].sort((a, b) => claimPriority(a) - claimPriority(b))[0];
  const primarySegmentId = primaryClaim?.trigger?.segment_ids?.[0] ?? topics[0]?.evidenceSegmentId ?? null;
  const primarySegment = bundle.transcript.find((segment) => segment.id === primarySegmentId);

  return {
    primaryTakeaway: {
      text: compact(primaryClaim?.headline ?? topics[0]?.takeaway ?? "No executive takeaway was recorded."),
      evidenceSegmentId: primarySegmentId,
      evidenceStartMs: primarySegment?.start_ms ?? topics[0]?.evidenceStartMs ?? null,
    },
    topics,
    followUps,
    counts: {
      total: followUps.length,
      outstanding,
      resolved,
    },
  };
}
