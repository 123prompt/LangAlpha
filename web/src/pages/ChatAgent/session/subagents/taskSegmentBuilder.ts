/**
 * The one place a `subagent_task` segment and its `subagentTasks` record are
 * derived from a tool call. Live streaming and history replay both build the
 * inline card through here, so a reloaded thread renders exactly what the live
 * turn did — the two used to carry copy-pasted branches that drifted silently.
 */
import type { SubagentTaskRecord, SubagentTaskSegment } from '@/types/chat';
import { normalizeAction } from '../../hooks/utils/eventUtils';
import { WORKFLOW_TASK_TYPE } from './workflowRunState';

interface ToolCallLike {
  name?: string;
  args?: Record<string, unknown>;
}

export interface TaskSegmentDerivation {
  segment: SubagentTaskSegment;
  record: SubagentTaskRecord;
  /** A resume stacks a *second* card on the same message: its segment is
   *  pushed unconditionally and its record replaces the previous one. Spawns
   *  and workflow runs are idempotent — one segment, merged in place. */
  stacks: boolean;
}

/**
 * Returns `null` for tool calls that render no inline card. `order` is the
 * caller's already-resolved segment order (event id or counter).
 */
export function deriveTaskSegment(
  toolCall: ToolCallLike,
  toolCallId: string,
  order: number,
): TaskSegmentDerivation | null {
  const args = toolCall.args || {};
  const description = (args.description as string) || '';

  if (toolCall.name === 'RunWorkflow') {
    // Workflow run launch — same segment/record machinery as a Task spawn,
    // discriminated by type 'workflow' at render time.
    return {
      segment: { type: 'subagent_task', subagentId: toolCallId, order },
      record: {
        subagentId: toolCallId,
        description: description || (args.workflow as string) || '',
        prompt: description,
        type: WORKFLOW_TASK_TYPE,
        action: 'init',
        status: 'running',
      },
      stacks: false,
    };
  }

  // Backend uses PascalCase "Task"; accept both for compatibility.
  if (toolCall.name !== 'task' && toolCall.name !== 'Task') return null;

  const action = normalizeAction((args.action as string) || (args.task_id ? 'resume' : 'init'));
  const prompt = (args.prompt as string) || description;
  const subagentType = (args.subagent_type as string) || 'general-purpose';

  if (action === 'init') {
    return {
      segment: { type: 'subagent_task', subagentId: toolCallId, order },
      record: {
        subagentId: toolCallId,
        description,
        prompt,
        type: subagentType,
        action: 'init',
        status: 'running',
      },
      stacks: false,
    };
  }

  // Resume/follow-up call — a new card with a "resumed" indicator, pointing at
  // the original task. Normalized to "task:xxx" to match floating card keys.
  const rawTargetId = (args.task_id as string) || '';
  const resumeTargetId = rawTargetId.startsWith('task:') ? rawTargetId : `task:${rawTargetId}`;
  return {
    segment: { type: 'subagent_task', subagentId: toolCallId, resumeTargetId, order },
    record: {
      subagentId: toolCallId,
      resumeTargetId,
      description,
      prompt,
      type: subagentType,
      action,
      status: 'running',
    },
    stacks: true,
  };
}

/**
 * Apply a derivation in place onto a message's segment list + task map, honoring
 * the stack-vs-merge rule. The two collections are the loose message bags the
 * stream handlers thread around, so this is the one place the typed derivation
 * is widened back into them.
 */
export function applyTaskSegment(
  derived: TaskSegmentDerivation,
  toolCallId: string,
  contentSegments: Record<string, unknown>[],
  subagentTasks: Record<string, Record<string, unknown>>,
): void {
  const exists = contentSegments.some(
    (s) => s.type === 'subagent_task' && s.subagentId === toolCallId
  );
  if (derived.stacks || !exists) {
    contentSegments.push({ ...derived.segment });
  }
  subagentTasks[toolCallId] = derived.stacks
    ? { ...derived.record }
    : { ...(subagentTasks[toolCallId] || {}), ...derived.record };
}
