/**
 * Paragraph delivery still has to look alive.
 *
 * The streaming indicator fades while text is landing, because the text is the
 * liveness signal, and fades back in during a pause. Under paragraph delivery
 * the arriving tokens are not visible progress, so the bubble has to treat a
 * withheld tail as a pause. The condition used to ask whether the whole bubble
 * was invisible text, which went false for the rest of a reply as soon as the
 * first paragraph landed and was never true once the turn called a tool.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MessageBubble } from '../MessageBubble';
import { MessageActionsProvider } from '../MessageActionsContext';
import { TranscriptDisplayContext } from '@/lib/transcriptDisplay';
import type { MessageRecord } from '../types';

vi.mock('@/hooks/useUser', () => ({ useUser: () => ({ user: null }) }));
vi.mock('@/contexts/ThemeContext', () => ({ useTheme: () => ({ theme: 'light' }) }));
vi.mock('../../Markdown', () => ({
  default: ({ content }: { content: string }) => <div data-testid="markdown-content">{content}</div>,
}));

/** A released paragraph, then a tail with no blank line after it yet. */
const RELEASED_THEN_HELD = 'The first paragraph is complete.\n\nThe second one is still being';

function bubble(message: Record<string, unknown>) {
  return (
    <TranscriptDisplayContext.Provider value={{ turnDisplay: 'lean', streamingMode: 'paragraph' }}>
      <MessageActionsProvider actions={{}}>
        <MessageBubble message={message as MessageRecord} turnIndex={0} isTurnTail isTurnLive />
      </MessageActionsProvider>
    </TranscriptDisplayContext.Provider>
  );
}

/**
 * The indicator starts quiet, so a static render says nothing: `arrivalQuiet`
 * alone would hold it visible and the assertion would pass on any
 * implementation. Land a token first, by bumping `arrivalSeq` the way the
 * stream does, and read it while that arrival is still fresh. Only the
 * paragraph condition can hold it visible then.
 */
function renderMidArrival(before: Record<string, unknown>, after: Record<string, unknown>) {
  const view = render(bubble(before));
  view.rerender(bubble(after));
  return view;
}

const quiet = (c: HTMLElement) =>
  c.querySelector('[data-testid="streaming-indicator"]')?.getAttribute('data-quiet');

describe('streaming indicator under paragraph delivery', () => {
  it('stays visible while the tail of a later paragraph is held back', () => {
    const base = {
      id: 'a0', role: 'assistant', contentType: 'text', timestamp: new Date(), isStreaming: true,
      reasoningProcesses: {}, toolCallProcesses: {},
    };
    const { container } = renderMidArrival(
      { ...base, arrivalSeq: 1, content: 'The first paragraph is complete.\n\n',
        contentSegments: [{ type: 'text', content: 'The first paragraph is complete.\n\n', order: 0 }] },
      { ...base, arrivalSeq: 2, content: RELEASED_THEN_HELD,
        contentSegments: [{ type: 'text', content: RELEASED_THEN_HELD, order: 0 }] },
    );

    expect(quiet(container)).toBe('true');
  });

  it('stays visible when the turn has also called a tool', () => {
    const tool = {
      tc1: {
        toolName: 'WebSearch', toolCall: { args: {} },
        isInProgress: false, isComplete: true, isFailed: false, order: 0,
      },
    };
    const base = {
      id: 'a1', role: 'assistant', contentType: 'text', timestamp: new Date(), isStreaming: true,
      reasoningProcesses: {}, toolCallProcesses: tool,
    };
    const segments = (text: string) => [
      { type: 'tool_call', order: 0, toolCallId: 'tc1' },
      { type: 'text', content: text, order: 1 },
    ];
    const { container } = renderMidArrival(
      { ...base, arrivalSeq: 1, content: 'The first paragraph is complete.\n\n',
        contentSegments: segments('The first paragraph is complete.\n\n') },
      { ...base, arrivalSeq: 2, content: RELEASED_THEN_HELD, contentSegments: segments(RELEASED_THEN_HELD) },
    );

    expect(quiet(container)).toBe('true');
  });

  it('fades once everything that arrived is released', () => {
    const whole = 'The first paragraph is complete.\n\nSo is the second one.\n\n';
    const base = {
      id: 'a2', role: 'assistant', contentType: 'text', timestamp: new Date(), isStreaming: true,
      reasoningProcesses: {}, toolCallProcesses: {},
    };
    const { container } = renderMidArrival(
      { ...base, arrivalSeq: 1, content: 'The first paragraph is complete.\n\n',
        contentSegments: [{ type: 'text', content: 'The first paragraph is complete.\n\n', order: 0 }] },
      { ...base, arrivalSeq: 2, content: whole,
        contentSegments: [{ type: 'text', content: whole, order: 0 }] },
    );

    expect(quiet(container)).toBe('false');
  });
});
