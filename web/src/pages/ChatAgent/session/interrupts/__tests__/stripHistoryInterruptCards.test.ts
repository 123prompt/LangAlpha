/**
 * The reconnect strip has to remove the card the redelivery will replace.
 *
 * A re-raised interrupt restores its card on the bubble that first rendered it,
 * while its pending entry is queued against the bubble the re-raise rode. The
 * strip runs off those entries and releases the interrupt id for the reconnect
 * stream, so a strip that misses the card leaves the history copy standing
 * beside the redelivered one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { AssistantMessage } from '@/types/chat';

const api = vi.hoisted(() => ({ replayThreadHistory: vi.fn() }));
vi.mock('../../../utils/api', () => ({ replayThreadHistory: api.replayThreadHistory }));

import { loadConversationHistory } from '../../history/replayHistory';
import { stripHistoryInterruptCards } from '../buckets';
import type { MessageRecord } from '../../types';
import { buildRuntime, makeDeps, replayOf } from '../../history/__tests__/replayHarness';

const PAUSE_REQUEST = [{ type: 'credit_pause', message: 'Out of credits.' }];

/** The thread as it replays after a resume failed and the graph re-raised. */
const RE_RAISED_THREAD = [
  { event: 'user_message', data: { thread_id: 'thread-1', turn_index: 0, content: 'Analyse the filing' } },
  {
    event: 'interrupt',
    data: { thread_id: 'thread-1', turn_index: 0, interrupt_id: 'int-1', action_requests: PAUSE_REQUEST },
  },
  {
    event: 'user_message',
    data: {
      thread_id: 'thread-1', turn_index: 1, run_id: 'run-1', content: '',
      metadata: { hitl_interrupt_ids: ['int-1'] },
    },
  },
  {
    event: 'interrupt',
    data: { thread_id: 'thread-1', turn_index: 1, interrupt_id: 'int-1', action_requests: PAUSE_REQUEST },
  },
];

function pauseSegments(messages: MessageRecord[]) {
  return (messages.filter((m) => m.role === 'assistant') as unknown as AssistantMessage[])
    .flatMap((b) => (b.contentSegments || []).filter((sg) => sg.type === 'credit_pause'));
}

function pauseCards(messages: MessageRecord[]) {
  return (messages.filter((m) => m.role === 'assistant') as unknown as AssistantMessage[])
    .flatMap((b) => Object.keys(b.creditPauses || {}));
}

beforeEach(() => vi.clearAllMocks());

describe('reconnect strip of replayed interrupt cards', () => {
  it('removes a re-raised card whose entry names a different bubble', async () => {
    const { rt, read } = buildRuntime();
    api.replayThreadHistory.mockImplementation(replayOf(RE_RAISED_THREAD));
    await loadConversationHistory(rt, makeDeps());

    const stripList = rt.unresolvedHistoryInterruptRef.current;
    expect(stripList).toHaveLength(1);
    // The entry rode the resume turn's bubble; the card stayed on the first.
    expect(stripList[0].assistantMessageId).toBe('history-assistant-1');
    expect(pauseSegments(read())).toHaveLength(1);

    const stripped = stripHistoryInterruptCards(read(), stripList);

    // Nothing left for the reconnect's redelivery to duplicate.
    expect(pauseSegments(stripped)).toHaveLength(0);
    expect(pauseCards(stripped)).toEqual([]);
  });

  it('strips by card id, and leaves cards no entry names', () => {
    const messages: MessageRecord[] = [
      {
        id: 'bubble-1',
        role: 'assistant',
        content: '',
        contentSegments: [
          { type: 'credit_pause', proposalId: 'int-1', order: 0 },
          { type: 'credit_pause', proposalId: 'int-2', order: 1 },
        ],
        creditPauses: {
          'int-1': { status: 'pending' },
          'int-2': { status: 'pending' },
        },
      } as unknown as MessageRecord,
    ];

    const stripped = stripHistoryInterruptCards(messages, [
      { type: 'credit_pause', assistantMessageId: 'bubble-9', proposalId: 'int-1', interruptId: 'int-1' },
    ]);

    // The named card goes even though its entry points at another bubble; the
    // unnamed one stays, because the strip is the pending set and nothing more.
    expect(pauseCards(stripped)).toEqual(['int-2']);
  });
});
