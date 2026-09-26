import {
  categorizeTool,
  getActiveLabel,
  getCompletedRowTitle,
  getCompletedSummary,
  type ToolCategory,
} from '../toolDisplayConfig';
import { formatThoughtFor } from '@/lib/elapsed';

/** Translation function signature compatible with i18next's t(). */
type TFn = (key: string, opts?: Record<string, unknown>) => string;

import type { ActivityItem, ToolActivityItem } from './activityTypes';
export type { ActivityItem, LiveState } from './activityTypes';

/**
 * Slot = what the header emits. `memory` and `profile` each collapse read+write
 * into a single fragment whose verb flips on any write (each is conceptually
 * one surface, the user's memory, the user's profile data). `fileRead`/
 * `fileEdit` and `memo`/`memoWrite` stay separate: distinct file/memo paths
 * shouldn't collide under one label, and any memo modification is surfaced
 * distinctly so a future regression letting the agent mutate a memo is visible.
 */
export type SummarySlot =
  | 'skill' | 'memory' | 'memo' | 'memoWrite' | 'profile' | 'code'
  | 'web' | 'search' | 'fileRead' | 'fileEdit' | 'reasoning' | 'generic';

export interface SummaryFragment {
  slot: SummarySlot;
  /** Reasoning only: the summed duration, when every thought knew its own. */
  thoughtMs?: number;
  count: number;
  modified?: boolean;
}

const SLOT_ORDER: SummarySlot[] = [
  'skill', 'memory', 'memo', 'memoWrite', 'profile', 'code',
  'web', 'search', 'fileRead', 'fileEdit', 'reasoning', 'generic',
];

/** When folded, cap the breakdown so the header stays scannable on a turn with
 *  many tool categories; expanding reveals the full list. */
const FOLDED_MAX = 3;

/** The counting bucket one item falls in: its tool category, or `reasoning`. */
export function classifyToolCall(item: ActivityItem): ToolCategory | 'reasoning' {
  return item.type === 'reasoning'
    ? 'reasoning'
    : categorizeTool(item.toolName || '', item.toolCall);
}

/** Per-category counts for a run, in the order the header reads them. */
export function summaryFragments(items: ActivityItem[]): SummaryFragment[] {
  const counts = new Map<ToolCategory | 'reasoning', number>();
  for (const item of items) {
    const key = classifyToolCall(item);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const memReads = counts.get('memoryRead') ?? 0;
  const memWrites = counts.get('memoryWrite') ?? 0;
  const memTotal = memReads + memWrites;
  const profileReads = counts.get('profileRead') ?? 0;
  const profileWrites = counts.get('profileWrite') ?? 0;
  const profileTotal = profileReads + profileWrites;

  const out: SummaryFragment[] = [];
  for (const slot of SLOT_ORDER) {
    if (slot === 'memory') {
      if (memTotal > 0) out.push({ slot, count: memTotal, modified: memWrites > 0 });
    } else if (slot === 'profile') {
      if (profileTotal > 0) out.push({ slot, count: profileTotal, modified: profileWrites > 0 });
    } else if (slot === 'reasoning' && counts.has('reasoning')) {
      out.push({ slot, count: counts.get('reasoning')!, thoughtMs: thoughtDuration(items) });
    } else if (counts.has(slot as ToolCategory | 'reasoning')) {
      out.push({ slot, count: counts.get(slot as ToolCategory | 'reasoning')! });
    }
  }
  return out;
}

/** Summed thinking time of a run's reasoning items, or undefined when any of
 *  them (a checkpoint-sourced replay, an older stored turn) has no duration:
 *  a partial sum would read as the whole. */
export function thoughtDuration(items: ActivityItem[]): number | undefined {
  let total = 0;
  for (const item of items) {
    if (item.type !== 'reasoning') continue;
    const ms = item.reasoningElapsedMs;
    if (typeof ms !== 'number') return undefined;
    total += ms;
  }
  return total;
}

function fragmentLabel(f: SummaryFragment, t: TFn): string {
  if (f.slot === 'reasoning') {
    return f.thoughtMs !== undefined
      ? t('toolArtifact.thoughtFor', { duration: formatThoughtFor(f.thoughtMs, t) })
      : t('toolArtifact.nReasoning', { count: f.count });
  }
  if (f.slot === 'skill') return t('toolArtifact.categoryCount.skill', { count: f.count });
  // No count for memory or profile: each is one surface, and any write/edit
  // overrules the pure-read framing.
  if (f.slot === 'memory') {
    return t(f.modified ? 'toolArtifact.categoryCount.memoryUpdated' : 'toolArtifact.categoryCount.memoryRead');
  }
  if (f.slot === 'profile') {
    return t(f.modified ? 'toolArtifact.categoryCount.profileUpdated' : 'toolArtifact.categoryCount.profileRead');
  }
  if (f.slot === 'fileRead') return t('toolArtifact.categoryCount.fileRead', { count: f.count });
  if (f.slot === 'fileEdit') return t('toolArtifact.categoryCount.fileEdit', { count: f.count });
  if (f.slot === 'memo') return t('toolArtifact.categoryCount.memo', { count: f.count });
  if (f.slot === 'memoWrite') return t('toolArtifact.categoryCount.memoWrite', { count: f.count });
  if (f.slot === 'code') return t('toolArtifact.categoryCount.code', { count: f.count });
  if (f.slot === 'web') return t('toolArtifact.categoryCount.web', { count: f.count });
  if (f.slot === 'search') return t('toolArtifact.categoryCount.search', { count: f.count });
  return t('toolArtifact.categoryCount.generic', { count: f.count });
}

/**
 * The run's one-line account of itself, e.g. "Ran 2 searches · read 3 files and
 * more". `expanded` drops the fold cap, since the rows themselves are on screen.
 */
export function summarizeCompletedItems(
  items: ActivityItem[],
  t: TFn,
  options?: { expanded?: boolean },
): string | undefined {
  const fragments = summaryFragments(items);
  let label: string | undefined;

  const reasoning = fragments.find((f) => f.slot === 'reasoning');
  if (fragments.length > 0 && fragments.some((f) => f.slot !== 'reasoning' && f.slot !== 'generic')) {
    // High-signal fragments (memory writes, memo writes) get priority: they are
    // information the user specifically needs and shouldn't drop into the
    // "and more …" tail when 4+ categories are present. Hoisted to the front of
    // the visible slice, preserving the relative order of everything else.
    const isPriority = (f: SummaryFragment) =>
      f.slot === 'memoWrite'
      || (f.slot === 'memory' && f.modified === true)
      || (f.slot === 'profile' && f.modified === true);
    const ordered = [...fragments.filter(isPriority), ...fragments.filter((f) => !isPriority(f))];
    const overflowing = !options?.expanded && ordered.length > FOLDED_MAX;
    const visible = overflowing ? ordered.slice(0, FOLDED_MAX) : ordered;
    label = visible.map((f) => fragmentLabel(f, t)).join(' · ');
    if (overflowing) label = `${label} ${t('toolArtifact.andMore')}`;
  } else if (fragments.length === 1 && reasoning?.thoughtMs !== undefined) {
    // A run that was only thought: say how long, not how many.
    label = fragmentLabel(reasoning, t);
  } else if (items.length > 0) {
    label = t('toolArtifact.nStepsCompleted', { count: items.length });
  }

  // Capitalize the first character only — embedded proper nouns and casing
  // stay intact. Applies to the fragment label and the step-count fallback.
  if (label && label.length > 0) label = label.charAt(0).toUpperCase() + label.slice(1);
  return label;
}

/** Present tense, for a call that is still running: "Reading memory about …". */
export function liveToolLabel(item: ToolActivityItem, t: TFn): string {
  return getActiveLabel(item.toolName || '', item.toolCall, t);
}

/** Past tense, for a settled row: "Read memory", "Activated skill". */
export function completedToolTitle(item: ToolActivityItem, t: TFn): string {
  return getCompletedRowTitle(item.toolName || '', item.toolCall, t, item.toolCallResult?.artifact);
}

/** The object a settled row names, rendered as its pill; null when there is none. */
export function completedToolSummary(item: ToolActivityItem, t: TFn): string | null {
  return getCompletedSummary(item.toolName || '', item.toolCall, t);
}
