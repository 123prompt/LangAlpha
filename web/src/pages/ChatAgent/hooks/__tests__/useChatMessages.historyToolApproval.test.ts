/**
 * A stored, unanswered tool approval must not arm the composer's pending slot.
 *
 * Nothing raises a tool approval any more and nothing answers one, so a thread
 * that stopped on one before that replays a card with no controls. Arming it as
 * `pendingInterrupt` disables both composers against that card, and a reload
 * repeats it: the thread is stranded. The card still renders as a record. A
 * credit pause on the same branch keeps re-arming, because Resume still answers
 * it.
 *
 * Drives the REAL hook (api module mocked), the way the dedup suite does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';
import { settleMountEffect } from './chatHookHarness';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', async () => (await import('./chatHookHarness')).apiMockModule());

import { getWorkflowStatus, replayThreadHistory, reconnectToWorkflowStream } from '../../utils/api';
import { useChatMessages } from '../useChatMessages';
import type { AssistantMessage, ContentSegment } from '@/types/chat';

const mockStatus = getWorkflowStatus as Mock;
const mockReplay = replayThreadHistory as Mock;
const mockReconnect = reconnectToWorkflowStream as Mock;

/** The stop as a live-order approval persisted it: one direct MCP call. */
const ORDER_REQUEST = [{ name: 'mcp__moomoo__place_order', args: { symbol: 'AAPL', qty: 1 } }];
const PAUSE_REQUEST = [{ type: 'credit_pause', message: 'Out of credits.' }];

function replayStoppedOn(actionRequests: unknown[]) {
  return (_tid: string, onEvent: (e: Record<string, unknown>) => void) => {
    onEvent({ event: 'user_message', turn_index: 0, content: 'buy one share', role: 'user' });
    onEvent({ event: 'interrupt', turn_index: 0, interrupt_id: 'int-1', action_requests: actionRequests });
    return Promise.resolve();
  };
}

function segmentsOf(messages: readonly unknown[], type: string): ContentSegment[] {
  return messages
    .filter((m): m is AssistantMessage => (m as AssistantMessage).role === 'assistant')
    .flatMap((m) => ((m.contentSegments as ContentSegment[] | undefined) || []).filter((s) => s.type === type));
}

describe('useChatMessages: unanswered interrupts from history on a paused thread', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReplay.mockReset();
    mockStatus.mockReset();
    // Paused: the run is over, so the reconnect branch is not the one taken.
    mockStatus.mockResolvedValue({ can_reconnect: false, status: 'completed', active_tasks: [] });
    mockReconnect.mockReset();
    mockReconnect.mockResolvedValue({ disconnected: false, aborted: false });
  });

  it('renders a stored tool approval as a record and leaves the composer open', async () => {
    mockReplay.mockImplementation(replayStoppedOn(ORDER_REQUEST));

    const { result } = renderHookWithProviders(() => useChatMessages('ws-x', 'th-x'));
    await waitFor(() => expect(mockReplay).toHaveBeenCalled());
    await settleMountEffect();

    await waitFor(() => expect(segmentsOf(result.current.messages, 'tool_approval')).toHaveLength(1));
    // Not armed: `pendingInterrupt` is what disables both composers.
    expect(result.current.pendingInterrupt).toBeNull();
  });

  // The active-run branch strips the replayed card and lets the reconnect
  // stream redeliver the interrupt through the LIVE projection, so a filter on
  // the paused branch alone still arms it here.
  describe('when the run is still active and the reconnect stream redelivers the stop', () => {
    function redeliver(actionRequests: unknown[]) {
      mockStatus.mockResolvedValue({
        can_reconnect: true, status: 'running', run_id: 'run-1', active_tasks: [], pending_report_back: false,
      });
      mockReplay.mockImplementation(replayStoppedOn(actionRequests));
      mockReconnect.mockImplementation(async (...args: unknown[]) => {
        const onEvent = args[3] as (e: Record<string, unknown>) => void;
        onEvent({ event: 'interrupt', interrupt_id: 'int-1', action_requests: actionRequests });
        return { disconnected: false, aborted: false };
      });
    }

    it('renders the redelivered tool approval as a record and leaves the composer open', async () => {
      redeliver(ORDER_REQUEST);

      const { result } = renderHookWithProviders(() => useChatMessages('ws-x', 'th-x'));
      await waitFor(() => expect(mockReconnect).toHaveBeenCalled());
      await settleMountEffect();

      await waitFor(() => expect(segmentsOf(result.current.messages, 'tool_approval')).toHaveLength(1));
      expect(result.current.pendingInterrupt).toBeNull();
    });

    it('still arms a redelivered credit pause', async () => {
      redeliver(PAUSE_REQUEST);

      const { result } = renderHookWithProviders(() => useChatMessages('ws-x', 'th-x'));
      await waitFor(() => expect(mockReconnect).toHaveBeenCalled());
      await settleMountEffect();

      await waitFor(() => expect(result.current.pendingInterrupt?.type).toBe('credit_pause'));
      expect(result.current.pendingInterrupt?.interruptId).toBe('int-1');
    });
  });

  it('still re-arms a stored credit pause, which Resume can answer', async () => {
    mockReplay.mockImplementation(replayStoppedOn(PAUSE_REQUEST));

    const { result } = renderHookWithProviders(() => useChatMessages('ws-x', 'th-x'));
    await waitFor(() => expect(mockReplay).toHaveBeenCalled());
    await settleMountEffect();

    await waitFor(() => expect(result.current.pendingInterrupt?.type).toBe('credit_pause'));
    expect(result.current.pendingInterrupt?.interruptId).toBe('int-1');
  });
});
