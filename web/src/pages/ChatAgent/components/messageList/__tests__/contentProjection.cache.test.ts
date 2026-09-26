/**
 * The projection cache has to answer for the message as it is now.
 *
 * Message identity alone does not carry that: the subagent tool-call and
 * tool-call-result handlers assign `contentSegments` and `toolCallProcesses`
 * onto the existing record and hand React a new array around the same object.
 * A cache keyed on the record answered the first tool call with the text-only
 * projection that preceded it, and that projection carries `nextExpiry: null`,
 * so nothing would ever retire it.
 */
import { describe, it, expect } from 'vitest';
import { projectMessageContent } from '../contentProjection';
import type { MessageRecord } from '../types';

const textOnly = (): MessageRecord => ({
  id: 'a0',
  role: 'assistant',
  content: 'Looking into it.',
  isStreaming: true,
  contentSegments: [{ type: 'text', content: 'Looking into it.', order: 0 }],
  reasoningProcesses: {},
  toolCallProcesses: {},
});

describe('projectMessageContent cache', () => {
  it('re-projects when a handler assigns new segments onto the same record', () => {
    const message = textOnly();
    const first = projectMessageContent(message, true);
    expect(first.blocks.map((b) => b.type)).toEqual(['text']);
    // Permanent by itself: nothing about a settled projection expires.
    expect(first.nextExpiry).toBeNull();

    // Verbatim shape of what handleSubagentToolCalls writes.
    message.contentSegments = [
      { type: 'text', content: 'Looking into it.', order: 0 },
      { type: 'tool_call', toolCallId: 'tc1', order: 1 },
    ];
    message.toolCallProcesses = {
      tc1: {
        toolName: 'Read', toolCall: { args: {} },
        isInProgress: true, isComplete: false, order: 1, _createdAt: Date.now(),
      },
    };

    const second = projectMessageContent(message, true);
    expect(second).not.toBe(first);
    expect(second.blocks.some((b) => b.type === 'activity')).toBe(true);
  });

  it('still returns the cached projection when nothing about the record moved', () => {
    const message = textOnly();
    expect(projectMessageContent(message, true)).toBe(projectMessageContent(message, true));
  });

  it('re-projects when the record stops streaming', () => {
    const message = textOnly();
    const streaming = projectMessageContent(message, true);
    message.isStreaming = false;
    expect(projectMessageContent(message, true)).not.toBe(streaming);
  });
});
