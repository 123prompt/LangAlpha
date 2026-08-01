import React from 'react';
import type { SubagentTaskRecord } from '@/types/chat';
import SubagentTaskMessageContent, { type ToolCallProcess } from '../SubagentTaskMessageContent';
import WorkflowRunCard from '../WorkflowRunCard';
import { WORKFLOW_TASK_TYPE } from '../../session/subagents/workflowRunState';
import type { MessageRecord, SubagentInfo, ToolCallProcessRecord } from './types';

/**
 * The card a `subagent_task` segment renders. A workflow run and a subagent
 * spawn share the segment type and are told apart only here, so the two render
 * paths (block-based and segment-based) enter the choice once instead of each
 * carrying its own copy.
 */
export function TaskSegmentCard({
  subagentId,
  task,
  toolCallProcess,
  onOpen,
  onDetailOpen,
}: {
  subagentId: string;
  /** The message's record for this segment; absent while a card is still
   *  being derived, in which case nothing renders. */
  task: MessageRecord | undefined;
  toolCallProcess?: ToolCallProcessRecord;
  onOpen?: (info: SubagentInfo) => void;
  /** Opens the raw tool result; only the transcript path offers it. */
  onDetailOpen?: (proc: ToolCallProcessRecord) => void;
}): React.ReactElement | null {
  if (!task) return null;
  const record = task as unknown as SubagentTaskRecord;

  if (record.type === WORKFLOW_TASK_TYPE) {
    return (
      <WorkflowRunCard
        subagentId={subagentId}
        description={record.description}
        status={record.status}
        onOpen={onOpen}
      />
    );
  }

  // The "View subagent output" affordance reads the task's own outcome, not the
  // Task call's immediate return — so the result and status ride along here.
  const enrichedProcess = toolCallProcess
    ? ({
        ...toolCallProcess,
        _subagentResult: record.result || null,
        _subagentStatus: record.status || null,
      } as ToolCallProcess)
    : undefined;

  return (
    <SubagentTaskMessageContent
      subagentId={subagentId}
      description={record.description}
      type={record.type}
      status={record.status}
      action={record.action}
      resumeTargetId={record.resumeTargetId}
      onOpen={onOpen}
      onDetailOpen={onDetailOpen}
      toolCallProcess={enrichedProcess}
    />
  );
}

export default TaskSegmentCard;
