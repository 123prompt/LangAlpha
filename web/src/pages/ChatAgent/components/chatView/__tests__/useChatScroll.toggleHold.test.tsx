/**
 * Opening a row under a streaming turn is a reader choosing a place. When the
 * hold that keeps the row under the pointer lets go, the follow must not carry
 * them to the bottom just because nothing scrolled while it held.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { useChatScroll } from '../useChatScroll';
import { announceAnchoredToggle } from '../../../utils/anchoredToggle';

const VIEW_H = 500;
let contentH = 2000;

const rect = (top: number, height: number) =>
  ({ top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: 0, toJSON: () => {} }) as DOMRect;

const measureViewport = (el: HTMLElement | null) => {
  if (!el) return;
  Object.defineProperty(el, 'clientHeight', { value: VIEW_H, configurable: true });
  Object.defineProperty(el, 'scrollHeight', { get: () => contentH, configurable: true });
  el.getBoundingClientRect = () => rect(0, VIEW_H);
};

// The row sits near the bottom of the view and its unfold grows below it, so
// its own top never moves and the hold has nothing to correct.
const measureRow = (el: HTMLElement | null) => {
  if (el) el.getBoundingClientRect = () => rect(VIEW_H - 80, 24);
};

let resize: (height: number) => void = () => {};

function Harness() {
  const api = useChatScroll({
    activeAgentId: 'main',
    messages: [{ id: 'a0' }],
    isActive: true,
    isActiveRef: { current: true },
    isLoadingHistory: false,
    isStreaming: true,
    currentThreadId: 't1',
    threadId: 't1',
  });
  return (
    <div ref={api.scrollAreaRef}>
      <div data-radix-scroll-area-viewport ref={measureViewport}>
        <div data-message-id="a0"><button data-row ref={measureRow}>row</button></div>
      </div>
    </div>
  );
}

const viewport = () => document.querySelector<HTMLElement>('[data-radix-scroll-area-viewport]')!;

/** Clamped the way a browser clamps, so a request past the end lands on it. */
function scrollTo(top: number) {
  const v = viewport();
  const clamped = Math.max(0, Math.min(top, contentH - VIEW_H));
  Object.defineProperty(v, 'scrollTop', { value: clamped, writable: true, configurable: true });
  v.dispatchEvent(new Event('scroll'));
}

describe('a disclosure opened near the bottom of a streaming turn', () => {
  const OriginalRO = window.ResizeObserver;

  beforeEach(() => {
    contentH = 2000;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    HTMLElement.prototype.scrollTo = function (this: HTMLElement, opts?: unknown) {
      const top = (opts as { top?: number } | undefined)?.top;
      if (typeof top === 'number' && this === viewport()) scrollTo(top);
    } as HTMLElement['scrollTo'];
    window.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        resize = (height) => cb([{ contentRect: { height } } as ResizeObserverEntry], this as unknown as ResizeObserver);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  });
  afterEach(() => {
    window.ResizeObserver = OriginalRO;
    vi.useRealTimers();
  });

  it('stays where the reader opened it once the hold lets go', async () => {
    render(<Harness />);
    act(() => { scrollTo(contentH - VIEW_H); });
    act(() => { resize(contentH); });

    act(() => { announceAnchoredToggle(document.querySelector<HTMLElement>('[data-row]')); });
    contentH += 400;
    act(() => { resize(contentH); });
    const opened = viewport().scrollTop;

    await act(async () => { vi.advanceTimersByTime(1100); });
    contentH += 20;
    act(() => { resize(contentH); });

    expect(viewport().scrollTop).toBe(opened);
  });

  it('keeps following when the hold leaves the reader at the bottom', async () => {
    render(<Harness />);
    act(() => { scrollTo(contentH - VIEW_H); });
    act(() => { resize(contentH); });

    // A collapse: the transcript shrinks and the view clamps onto the end.
    act(() => { announceAnchoredToggle(document.querySelector<HTMLElement>('[data-row]')); });
    contentH -= 200;
    act(() => { scrollTo(contentH - VIEW_H); });
    act(() => { resize(contentH); });

    await act(async () => { vi.advanceTimersByTime(1100); });
    contentH += 20;
    act(() => { resize(contentH); });

    expect(viewport().scrollTop).toBe(contentH - VIEW_H);
  });
});
