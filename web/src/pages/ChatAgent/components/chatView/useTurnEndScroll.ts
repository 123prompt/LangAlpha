import { useEffect, useRef } from 'react';
import type { TurnEndScroll } from '@/lib/turnEndScroll';
import { prefersReducedMotion } from '@/lib/reducedMotion';
import { findMessageElement } from '../../utils/scrollDom';
import type { useChatScroll } from './useChatScroll';
import type { ChatMessage } from '@/types/chat';

function lastAssistant(messages: ReadonlyArray<ChatMessage>): ChatMessage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') return messages[i];
  }
  return null;
}

type ScrollController = Pick<
  ReturnType<typeof useChatScroll>,
  'scrollAreaRef' | 'getScrollContainer' | 'pinToMessage' | 'pinTargetRef' | 'isNearBottomRef' | 'activeAgentIdRef' | 'entryRestoreSettled'
>;

/**
 * Turn completion (`isStreaming` true -> false, once per turn; an interrupt
 * keeps it true, the caller decides) under the
 * 'reply_start' preference: bring the first line of the final reply, the
 * bubble's last prose block, under the viewport top and stop following. Only a reader the follow was carrying is
 * moved; one who scrolled up keeps their place. pinToMessage decides the rest:
 * a reply shorter than the viewport clamps to the bottom and nothing moves,
 * and its settle window re-measures the anchor as late media lands.
 */
export function useTurnEndScroll(
  scroll: ScrollController,
  {
    messages,
    isStreaming,
    isActiveRef,
    turnEndScroll,
  }: {
    messages: ReadonlyArray<ChatMessage>;
    isStreaming: boolean;
    isActiveRef: { current: boolean };
    turnEndScroll: TurnEndScroll;
  },
) {
  const { scrollAreaRef, getScrollContainer, pinToMessage, pinTargetRef, isNearBottomRef, activeAgentIdRef, entryRestoreSettled } = scroll;
  const wasStreamingRef = useRef(isStreaming);
  // The last assistant bubble of the last idle render. A turn that produced
  // nothing (a regenerate or edit whose checkpoint fetch failed and put the
  // old transcript back) ends with that same bubble, and nothing has finished.
  const idleTailRef = useRef<ChatMessage | null | undefined>(undefined);
  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    wasStreamingRef.current = isStreaming;
    const tail = lastAssistant(messages);
    const idleTail = idleTailRef.current;
    if (!isStreaming) idleTailRef.current = tail;
    if (!wasStreaming || isStreaming || turnEndScroll !== 'reply_start') return;
    if (idleTail !== undefined && tail === idleTail) return;
    if (activeAgentIdRef.current !== 'main' || !isActiveRef.current) return;
    // A bottom pin is the follow inside its settle window (thread entry, the
    // jump pill) and hands over; an offset or anchor pin holds a place the
    // reader chose.
    if (pinTargetRef.current && pinTargetRef.current.mode !== 'bottom') return;
    if (!isNearBottomRef.current || !entryRestoreSettled()) return;
    const c = getScrollContainer(scrollAreaRef);
    if (!c) return;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === 'user') return; // the finishing turn has no rendered reply
      if (m.role !== 'assistant') continue;
      // Orphan (empty, settled) assistant bubbles stay in state but never render.
      if (!findMessageElement(c, m.id)) continue;
      pinToMessage(m.id, prefersReducedMotion() ? 'auto' : 'smooth', true, 'reply');
      return;
    }
  }, [isStreaming, turnEndScroll, messages, isActiveRef, scrollAreaRef, getScrollContainer, pinToMessage, pinTargetRef, isNearBottomRef, activeAgentIdRef, entryRestoreSettled]);
}
