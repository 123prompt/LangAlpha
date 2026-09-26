/**
 * Opening a deliverables deck under a streaming turn is a small scroll to a
 * chosen place. The reader who had stepped out of the follow to open it should
 * still be there afterwards.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { useChatScroll } from '../useChatScroll';

const VIEW_H = 500;
const CONTENT_H = 2000;

const rect = (top: number, height: number) =>
  ({ top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: 0, toJSON: () => {} }) as DOMRect;

/** jsdom lays nothing out, so the geometry the controller reads is stated here. */
const measureViewport = (el: HTMLElement | null) => {
  if (!el) return;
  Object.defineProperty(el, 'clientHeight', { value: VIEW_H, configurable: true });
  Object.defineProperty(el, 'scrollHeight', { value: CONTENT_H, configurable: true });
  el.getBoundingClientRect = () => rect(0, VIEW_H);
};

// The reader has paused the follow 200px short of the end, outside the 120px
// band. The deck hangs 100px below the fold, so revealing it moves exactly that
// far and lands 100px from the end, inside the band. That landing is the whole
// point of the test: it is the shape that used to be read as rejoining.
const PAUSED_AT = CONTENT_H - VIEW_H - 200;
const measureDeck = (el: HTMLElement | null) => {
  if (el) el.getBoundingClientRect = () => rect(VIEW_H - 12, 100);
};

let api: ReturnType<typeof useChatScroll>;

function Harness() {
  api = useChatScroll({
    activeAgentId: 'main',
    messages: [{ id: 'a0' }],
    isActive: true,
    isActiveRef: { current: true },
    isLoadingHistory: false,
    isStreaming: true,
    currentThreadId: 't1',
    threadId: 't1',
  });
  // Ref callbacks run at commit, ahead of the controller's own effects, which
  // measure on mount.
  return (
    <div ref={api.scrollAreaRef}>
      <div data-radix-scroll-area-viewport ref={measureViewport}>
        <div data-message-id="a0"><div className="turn-files" ref={measureDeck}>cards</div></div>
      </div>
    </div>
  );
}

const viewport = () => document.querySelector<HTMLElement>('[data-radix-scroll-area-viewport]')!;

function scrollTo(top: number) {
  const v = viewport();
  Object.defineProperty(v, 'scrollTop', { value: top, writable: true, configurable: true });
  v.dispatchEvent(new Event('scroll'));
}

/** The reader steps out of the follow, which is what makes the reveal matter. */
const leaveTheBottom = () => act(() => { scrollTo(PAUSED_AT); });

describe('revealing a deck under a streaming turn', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // jsdom has no scrollTo on an element; the controller's is the only caller.
    HTMLElement.prototype.scrollTo = function (this: HTMLElement, opts?: unknown) {
      const top = (opts as { top?: number } | undefined)?.top;
      if (typeof top === 'number' && this === viewport()) scrollTo(top);
    } as HTMLElement['scrollTo'];
  });
  afterEach(() => { vi.useRealTimers(); });

  it('leaves the follow paused, however near the bottom the cards sit', async () => {
    render(<Harness />);
    leaveTheBottom();
    expect(api.isNearBottomRef.current).toBe(false);

    await act(async () => { api.revealFiles('a0'); await Promise.resolve(); });

    // The pin owns this position, so its own scroll is not the reader saying
    // they rejoined the stream. Counted as one, the follow came back when the
    // settle window let go and took the cards off screen.
    expect(api.isNearBottomRef.current).toBe(false);
    expect(api.pinTargetRef.current).toMatchObject({ mode: 'reveal', id: 'a0' });
  });

  it('still lets an ordinary scroll to the bottom rejoin the stream', () => {
    render(<Harness />);
    leaveTheBottom();

    // The control: the same landing, reached by the reader rather than by a pin.
    act(() => { scrollTo(CONTENT_H - VIEW_H); });

    expect(api.isNearBottomRef.current).toBe(true);
  });
});
