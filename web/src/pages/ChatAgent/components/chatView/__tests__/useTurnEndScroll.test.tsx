import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useTurnEndScroll } from '../useTurnEndScroll';
import type { ChatMessage } from '@/types/chat';

vi.mock('../../../utils/scrollDom', () => ({
  findMessageElement: () => document.createElement('div'),
}));

function controller() {
  return {
    scrollAreaRef: { current: document.createElement('div') },
    getScrollContainer: () => document.createElement('div'),
    pinToMessage: vi.fn(),
    pinTargetRef: { current: null },
    isNearBottomRef: { current: true },
    activeAgentIdRef: { current: 'main' },
    entryRestoreSettled: () => true,
  };
}

const user = (id: string): ChatMessage => ({ id, role: 'user', content: 'q' } as ChatMessage);
const assistant = (id: string): ChatMessage => ({ id, role: 'assistant', content: 'a' } as ChatMessage);

function render(scroll: ReturnType<typeof controller>, initial: { messages: ChatMessage[]; isStreaming: boolean }) {
  return renderHook(
    (props: { messages: ChatMessage[]; isStreaming: boolean }) =>
      useTurnEndScroll(scroll, { ...props, isActiveRef: { current: true }, turnEndScroll: 'reply_start' }),
    { initialProps: initial },
  );
}

describe('useTurnEndScroll', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lands instantly when the reader prefers reduced motion', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce'), media: q, addEventListener() {}, removeEventListener() {} }));
    const scroll = controller();
    const before = [user('u1'), assistant('a1')];
    const { rerender } = render(scroll, { messages: before, isStreaming: false });
    const pending = [...before, user('u2'), assistant('a2')];
    rerender({ messages: pending, isStreaming: true });
    rerender({ messages: pending, isStreaming: false });
    expect(scroll.pinToMessage).toHaveBeenCalledWith('a2', 'auto', true, 'reply');
  });

  it('lands on the reply that the turn produced', () => {
    const scroll = controller();
    const before = [user('u1'), assistant('a1')];
    const { rerender } = render(scroll, { messages: before, isStreaming: false });
    const pending = [...before, user('u2'), assistant('a2')];
    rerender({ messages: pending, isStreaming: true });
    rerender({ messages: pending, isStreaming: false });
    expect(scroll.pinToMessage).toHaveBeenCalledWith('a2', 'smooth', true, 'reply');
  });

  it('stays put when a preflight rolls the old transcript back', () => {
    // A regenerate flips isStreaming on, replaces the reply with a placeholder,
    // then restores the snapshot when the checkpoint fetch fails: no turn ended.
    const scroll = controller();
    const before = [user('u1'), assistant('a1')];
    const { rerender } = render(scroll, { messages: before, isStreaming: false });
    rerender({ messages: [user('u1'), assistant('assistant-pending-1')], isStreaming: true });
    rerender({ messages: before, isStreaming: false });
    expect(scroll.pinToMessage).not.toHaveBeenCalled();
  });

  it('lands after a reload that opened mid-turn', () => {
    const scroll = controller();
    const streaming = [user('u1'), assistant('a1')];
    const { rerender } = render(scroll, { messages: streaming, isStreaming: true });
    rerender({ messages: [user('u1'), { ...assistant('a1') }], isStreaming: false });
    expect(scroll.pinToMessage).toHaveBeenCalledWith('a1', 'smooth', true, 'reply');
  });
});
