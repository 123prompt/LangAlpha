/**
 * Market chat renders through `MessageList` like every other transcript, so
 * its turns fold, and a fold measures the turn against the instant the client
 * saw it stop. Nothing here recorded that instant, so every settled market
 * turn showed a bare `Worked` with no duration.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

const sendFlashChatMessage = vi.fn();
vi.mock('../../utils/api', () => ({
  sendFlashChatMessage: (...args: unknown[]) => sendFlashChatMessage(...args),
}));

import { useMarketChat } from '../useMarketChat';

type Emit = (event: Record<string, unknown>) => void;

/** The arg position `useMarketChat` passes its event callback in. */
const ON_EVENT = 2;

beforeEach(() => {
  sendFlashChatMessage.mockReset();
});

async function settledAssistant(impl: (emit: Emit) => Promise<void>) {
  sendFlashChatMessage.mockImplementation(async (...args: unknown[]) => {
    await impl(args[ON_EVENT] as Emit);
  });
  const { result } = renderHook(() => useMarketChat());
  await act(async () => {
    await result.current.handleSendMessage('What is AAPL doing?').catch(() => {});
  });
  await waitFor(() => expect(result.current.isLoading).toBe(false));
  const assistant = result.current.messages.find((m) => m.role === 'assistant');
  expect(assistant).toBeDefined();
  return assistant!;
}

describe('useMarketChat turn completion', () => {
  it('records when a finished turn stopped', async () => {
    const assistant = await settledAssistant(async (emit) => {
      emit({ event: 'message_chunk', content_type: 'text', content: 'It is up 1.2%.' });
    });

    expect(assistant.isStreaming).toBe(false);
    expect(typeof assistant.completionObservedAt).toBe('number');
  });

  it('records when the stream threw', async () => {
    // The turn is over from the reader's side however it ended, and its fold
    // still has to say how long it ran.
    const assistant = await settledAssistant(async () => {
      throw Object.assign(new Error('upstream died'), { status: 502 });
    });

    expect(assistant.isStreaming).toBe(false);
    expect(typeof assistant.completionObservedAt).toBe('number');
  });

  it('carries the server reasoning duration onto the row', async () => {
    // The reasoning row reads `elapsedMs` for its "Thought for" label, the
    // same field every other transcript fills from `elapsed_ms`.
    const assistant = await settledAssistant(async (emit) => {
      emit({ event: 'message_chunk', content_type: 'reasoning_signal', content: 'start' });
      emit({ event: 'message_chunk', content_type: 'reasoning', content: 'Checking the tape.' });
      emit({ event: 'message_chunk', content_type: 'reasoning_signal', content: 'complete', elapsed_ms: 4200 });
      emit({ event: 'message_chunk', content_type: 'text', content: 'It is up 1.2%.' });
    });

    const [reasoning] = Object.values(assistant.reasoningProcesses ?? {});
    expect(reasoning).toMatchObject({ reasoningComplete: true, elapsedMs: 4200 });
  });
});
