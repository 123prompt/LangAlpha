import React from 'react';
import type { SubagentTaskRecord } from '@/types/chat';
import SubagentTaskMessageContent from '../SubagentTaskMessageContent';
import WorkflowRunCard from '../WorkflowRunCard';
import { WORKFLOW_TASK_TYPE } from '../../session/subagents/workflowRunState';
import type { SubagentInfo, ToolCallProcessRecord } from './types';

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
  task: SubagentTaskRecord | undefined;
  toolCallProcess?: ToolCallProcessRecord;
  onOpen?: (info: SubagentInfo) => void;
  /** Opens the spawn's detail by its call id; only the transcript path offers it. */
  onDetailOpen?: (toolCallId: string) => void;
}): React.ReactElement | null {
  if (!task) return null;

  if (task.type === WORKFLOW_TASK_TYPE) {
    return (
      <WorkflowRunCard
        subagentId={subagentId}
        description={task.description}
        status={task.status}
        launchReply={task.result}
        onOpen={onOpen}
      />
    );
  }

  return (
    <SubagentTaskMessageContent
      subagentId={subagentId}
      description={task.description}
      type={task.type}
      status={task.status}
      action={task.action}
      resumeTargetId={task.resumeTargetId}
      onOpen={onOpen}
      onDetailOpen={onDetailOpen}
      toolCallProcess={toolCallProcess}
    />
  );
}

export default TaskSegmentCard;
