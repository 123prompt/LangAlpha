import { useCallback } from 'react';
import type { ToolCallProcessRecord } from '../ToolCallDetailView';

interface TranscriptMessage {
  toolCallProcesses?: Record<string, unknown>;
  subagentTasks?: Record<string, { status?: string }>;
}

/**
 * A tool call's record as a transcript holds it now, so a surface that shows
 * one reads the live record rather than a copy taken at click time. Newest
 * first, so a regenerated turn's copy of a call wins over the one it replaced.
 *
 * A spawn's record settles as soon as the task is dispatched, long before the
 * task it started does; the outcome the detail view reports lives on the
 * message's task record and rides along as `_subagentStatus`.
 */
function findToolCallProcess(messages: readonly unknown[], toolCallId: string): ToolCallProcessRecord | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i] as TranscriptMessage | null | undefined;
    const proc = msg?.toolCallProcesses?.[toolCallId] as ToolCallProcessRecord | undefined;
    if (!proc) continue;
    const task = msg?.subagentTasks?.[toolCallId];
    return task ? { ...proc, _subagentStatus: task.status || null } : proc;
  }
  return undefined;
}

export const NO_TRANSCRIPTS: readonly (readonly unknown[])[] = [];

/**
 * The accessor a detail surface reads a tool call through. It is remade per
 * transcript change and read at render, so the surface shows a call's result
 * as it lands without holding a copy; a copy taken at click time would show a
 * running call forever, because the stream handlers replace a record rather
 * than mutate it. The accessor's identity is also what a memo downstream keys
 * a merge on. The main transcript is searched first, then each subagent's own
 * messages, so a row clicked inside a subagent transcript resolves too.
 */
export function useToolCallLookup(
  messages: readonly unknown[],
  subagentTranscripts: readonly (readonly unknown[])[] = NO_TRANSCRIPTS,
): (toolCallId: string) => ToolCallProcessRecord | undefined {
  return useCallback((toolCallId: string) => {
    const own = findToolCallProcess(messages, toolCallId);
    if (own) return own;
    for (const transcript of subagentTranscripts) {
      const proc = findToolCallProcess(transcript, toolCallId);
      if (proc) return proc;
    }
    return undefined;
  }, [messages, subagentTranscripts]);
}
