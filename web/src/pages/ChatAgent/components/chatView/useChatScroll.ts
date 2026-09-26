import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { isNearBottom } from '../../utils/scrollHelpers';
import { findMessageElement, resolveScrollContent, resolveScrollViewport } from '../../utils/scrollDom';
import { scrollMemory } from '@/lib/scrollMemory';
import { ANCHORED_TOGGLE_EVENT } from '../../utils/anchoredToggle';

// Scroll/pin tuning. Distance from the bottom (px) still counted as "at bottom";
// settle window the pin re-applies through as async media expands; fallback for
// engines without a `scrollend` event.
const NEAR_BOTTOM_PX = 120;
/** "At the bottom" for an upward scroll. Not 0: scrollHeight and clientHeight
 *  are rounded and scrollTop is not, and under browser zoom a container at
 *  its maximum reads a residual of a pixel or two. */
const AT_BOTTOM_PX = 4;
const SETTLE_QUIET_MS = 1500;
const SETTLE_HARD_CAP_MS = 8000;
const SCROLLEND_FALLBACK_MS = 600;
// Gap left above a bubble the transcript is pinned to.
const ANCHOR_OFFSET_PX = 16;
// How long a toggled row is held in place while its disclosure animates open
// or shut; longer than the fold spring, shorter than the next streamed chunk.
const TOGGLE_HOLD_MS = 1000;
// Breathing room left under a deliverables deck brought into view.
const REVEAL_GAP_PX = 12;

/** scrollTop that puts bubble `id` just under the viewport top, or null once it is no longer in the transcript. */
function anchorTop(c: HTMLElement, id: string, part?: AnchorPart): number | null {
  const msg = findMessageElement(c, id);
  if (!msg) return null;
  // The reply part is the bubble's last prose block, so a turn that opened with
  // commentary and tool rows lands on the answer; a bubble without prose is
  // its own start.
  const el = (part === 'reply' && msg.querySelector<HTMLElement>('[data-reply-start]')) || msg;
  return Math.max(0, c.scrollTop + el.getBoundingClientRect().top - c.getBoundingClientRect().top - ANCHOR_OFFSET_PX);
}

/** scrollTop that brings the deliverables deck on bubble `id` into view, capped
 *  so the deck's own top never leaves it: a deck taller than the viewport is
 *  read from its first card rather than chased past it. Null once the deck is
 *  gone, which is what retires the pin. */
function revealTop(c: HTMLElement, id: string): number | null {
  const msg = findMessageElement(c, id);
  const deck = msg?.querySelector<HTMLElement>('.turn-files');
  if (!deck) return null;
  const view = c.getBoundingClientRect();
  const rect = deck.getBoundingClientRect();
  // clientHeight, not the rect's own bottom: a horizontal scrollbar is not
  // viewport a card can be read in.
  const needed = Math.max(0, rect.bottom + REVEAL_GAP_PX - (view.top + c.clientHeight));
  const headroom = Math.max(0, rect.top - view.top);
  return c.scrollTop + Math.min(needed, headroom);
}

/** Chat transcript scroll controller + tab scroll memory (carved out of
 * ChatView, 5.9c): bottom pin with async-settle re-apply, per-frame streaming follow,
 * thread-entry restore, jump-to-latest pill, and per-tab scroll memory. */
/**
 * Pin controller state. 'bottom' follows the growing transcript end; 'offset'
 * converges on a remembered mid-thread scrollTop that async content (charts,
 * markdown, images) hasn't made reachable yet — same settle machinery,
 * different target. 'anchor' holds a chosen bubble under the viewport top
 * (minimap navigation), re-measured on every re-apply so media above it
 * finishing layout can't shift the landing.
 */
export type AnchorPart = 'reply';
export type PinTarget =
  | { mode: 'bottom' }
  | { mode: 'offset'; top: number }
  | { mode: 'anchor'; id: string; part?: AnchorPart }
  | { mode: 'reveal'; id: string };

export function useChatScroll({
  activeAgentId,
  messages,
  isActive,
  isActiveRef,
  isLoadingHistory,
  isStreaming,
  currentThreadId,
  threadId,
}: {
  activeAgentId: string;
  messages: unknown[];
  isActive: boolean;
  isActiveRef: { current: boolean };
  isLoadingHistory: boolean;
  /** A turn is open: the transcript grows on its own, so a reader at the end is carried along. */
  isStreaming: boolean;
  currentThreadId: string;
  threadId: string;
}) {
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const isStreamingRef = useRef(isStreaming);
  isStreamingRef.current = isStreaming;
  const subagentScrollAreaRef = useRef<HTMLDivElement>(null);

  // Resolved thread id for the cross-unmount scroll store (scrollMemory) — a
  // ref so the scroll listener always stamps the current thread without
  // re-binding. '__default__' (unresolved new thread) is never stored.
  const memoryTidRef = useRef<string | null>(null);
  const resolvedTid = currentThreadId || threadId;
  memoryTidRef.current = resolvedTid && resolvedTid !== '__default__' ? resolvedTid : null;

  // --- Scroll position memory for tab switching ---
  // Stores scrollTop per agentId so switching tabs preserves position
  const scrollPositionsRef = useRef<Record<string, number>>({});
  const activeAgentIdRef = useRef(activeAgentId);
  activeAgentIdRef.current = activeAgentId;
  // Flag to skip subagent auto-scroll when restoring a saved position
  const skipSubagentAutoScrollRef = useRef(false);

  // Helper: get the scrollable container from a ScrollArea ref
  const getScrollContainer = useCallback(
    (ref: React.RefObject<HTMLDivElement | null>): HTMLElement | null => resolveScrollViewport(ref?.current ?? null),
    [],
  );

  // Save scroll position of the currently active tab
  const saveScrollPosition = useCallback(() => {
    const currentId = activeAgentIdRef.current;
    const ref = currentId === 'main' ? scrollAreaRef : subagentScrollAreaRef;
    const container = getScrollContainer(ref);
    if (container) {
      scrollPositionsRef.current[currentId] = container.scrollTop;
    }
  }, [getScrollContainer]);

  // Restore scroll position after the new tab mounts
  useEffect(() => {
    const savedPosition = scrollPositionsRef.current[activeAgentId];
    if (savedPosition == null) return;

    // requestAnimationFrame waits for DOM commit + layout
    requestAnimationFrame(() => {
      const ref = activeAgentId === 'main' ? scrollAreaRef : subagentScrollAreaRef;
      const container = getScrollContainer(ref);
      if (container) {
        // Mark as programmatic so the main-tab scroll listener doesn't treat
        // this restore as a user scroll (which would cancel the pin / save).
        programmaticScrollRef.current = true;
        container.scrollTop = savedPosition;
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            programmaticScrollRef.current = false;
          }),
        );
      }
    });
  }, [activeAgentId, getScrollContainer]);

  // ==========================================================================
  // Chat transcript scroll controller
  // Reliable land-at-bottom that survives async content (charts/code/images)
  // expanding after the initial scroll, plus a jump-to-latest affordance.
  // See utils/scrollHelpers.
  // ==========================================================================

  // "Near bottom" trackers (used by streaming follow + the pin controller).
  const isNearBottomRef = useRef(true);
  const isSubagentNearBottomRef = useRef(true);

  const pinTargetRef = useRef<PinTarget | null>(null);
  const programmaticScrollRef = useRef(false);
  // Detaches the pending release of the current programmatic scroll (see
  // withProgrammaticScroll); null when no release is pending.
  const programmaticReleaseRef = useRef<(() => void) | null>(null);
  const settleQuietTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settleHardCapRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restoredForThreadRef = useRef<string | null>(null);
  // The entry-restore frame, tracked so a thread switch / unmount cancels a
  // pending scroll instead of yanking a now-stale view.
  const entryRestoreRafRef = useRef<number | null>(null);
  const visibilityRafRef = useRef<number | null>(null);
  // The last scrollTop the streaming follow set. Its scroll event is recognised
  // by position rather than by the programmatic flag: a follow runs on every
  // growth frame, and a flag re-armed that often never clears, which would
  // swallow a keyboard or scrollbar scroll for the whole turn. The flag stays
  // for smooth scrolls, which fire many events at positions nobody can predict.
  const followTopRef = useRef<number | null>(null);

  /** The entry restore for this thread has landed, so an automatic scroll may move the view. */
  const entryRestoreSettled = useCallback(
    () => !memoryTidRef.current || restoredForThreadRef.current === memoryTidRef.current,
    [],
  );
  /** A turn is streaming, nothing else owns the scroll and the reader is
   *  riding the end. Growth in a settled transcript is the reader's own doing
   *  (a block opened, a panel rewrapping the text) and is left where it is. */
  const isFollowing = useCallback(
    () => isStreamingRef.current && !pinTargetRef.current && isNearBottomRef.current && entryRestoreSettled(),
    [entryRestoreSettled],
  );

  // Jump-to-latest pill.
  const messagesLenRef = useRef(0);
  messagesLenRef.current = messages.length;
  const pillBaselineLenRef = useRef(0);
  const [jumpPill, setJumpPill] = useState<{ visible: boolean; hasNew: boolean; newCount: number }>({
    visible: false,
    hasNew: false,
    newCount: 0,
  });
  const setPillState = useCallback((next: { visible: boolean; hasNew: boolean; newCount: number }) => {
    setJumpPill((prev) =>
      prev.visible === next.visible && prev.hasNew === next.hasNew && prev.newCount === next.newCount
        ? prev
        : next,
    );
  }, []);
  // Wrap a programmatic scroll so the scroll listener doesn't mistake it for a
  // user scroll (which cancels the pin). Smooth scrolls clear on `scrollend`,
  // with a fallback that fires 600ms after the last scroll event: it measures
  // quiet, not elapsed time, because a whole-thread smooth scroll runs longer
  // than any fixed budget, and it still covers a scroll that never moves and
  // so never ends. Instant scrolls clear after the scroll event flushes (double
  // rAF). The flag is shared, so a newer scroll detaches the previous release
  // first: a release firing mid-way through this scroll would hand its
  // remaining scroll events to the user-scroll branch, which drops the pin
  // the new scroll just set.
  const withProgrammaticScroll = useCallback(
    (fn: () => void, behavior: 'auto' | 'smooth' = 'auto') => {
      programmaticReleaseRef.current?.();
      programmaticScrollRef.current = true;
      fn();
      if (behavior === 'smooth') {
        const c = getScrollContainer(scrollAreaRef);
        let timer: ReturnType<typeof setTimeout> | null = null;
        function detach() {
          c?.removeEventListener('scrollend', clear);
          c?.removeEventListener('scroll', arm);
          if (timer) clearTimeout(timer);
          if (programmaticReleaseRef.current === detach) programmaticReleaseRef.current = null;
        }
        function clear() {
          detach();
          programmaticScrollRef.current = false;
        }
        function arm() {
          if (timer) clearTimeout(timer);
          timer = setTimeout(clear, SCROLLEND_FALLBACK_MS);
        }
        c?.addEventListener('scrollend', clear, { once: true });
        c?.addEventListener('scroll', arm, { passive: true });
        arm();
        programmaticReleaseRef.current = detach;
      } else {
        let inner = 0;
        const outer = requestAnimationFrame(() => {
          inner = requestAnimationFrame(() => {
            programmaticReleaseRef.current = null;
            programmaticScrollRef.current = false;
          });
        });
        programmaticReleaseRef.current = () => {
          cancelAnimationFrame(outer);
          cancelAnimationFrame(inner);
          programmaticReleaseRef.current = null;
        };
      }
    },
    [getScrollContainer],
  );

  // The growing content node inside the fixed-height Radix viewport. The viewport
  // height is fixed (h-full); only its content grows as async media expands, so
  // that is what the ResizeObserver must watch.
  const getScrollContent = useCallback((c: HTMLElement): HTMLElement => resolveScrollContent(c), []);

  const clearSettleTimers = useCallback(() => {
    if (settleQuietTimerRef.current) {
      clearTimeout(settleQuietTimerRef.current);
      settleQuietTimerRef.current = null;
    }
    if (settleHardCapRef.current) {
      clearTimeout(settleHardCapRef.current);
      settleHardCapRef.current = null;
    }
  }, []);

  // Arm the settle window: re-pin while content keeps growing, give up after a
  // 1.5s quiet window (reset on each settle resize) or an 8s hard cap.
  const armSettleTimers = useCallback(() => {
    if (settleQuietTimerRef.current) clearTimeout(settleQuietTimerRef.current);
    settleQuietTimerRef.current = setTimeout(() => {
      // Quiet window elapsed — the settle session is over. Tear down BOTH timers
      // so the next pin session arms a fresh hard cap; otherwise it inherits this
      // session's stale (shortened or already-elapsed) one and gives up early.
      pinTargetRef.current = null;
      settleQuietTimerRef.current = null;
      if (settleHardCapRef.current) {
        clearTimeout(settleHardCapRef.current);
        settleHardCapRef.current = null;
      }
    }, SETTLE_QUIET_MS);
    if (!settleHardCapRef.current) {
      settleHardCapRef.current = setTimeout(() => {
        pinTargetRef.current = null;
        settleHardCapRef.current = null;
        if (settleQuietTimerRef.current) {
          clearTimeout(settleQuietTimerRef.current);
          settleQuietTimerRef.current = null;
        }
      }, SETTLE_HARD_CAP_MS);
    }
  }, []);

  const pinToBottom = useCallback(
    (behavior: 'auto' | 'smooth' = 'auto') => {
      const c = getScrollContainer(scrollAreaRef);
      if (!c) return;
      pinTargetRef.current = { mode: 'bottom' };
      isNearBottomRef.current = true;
      pillBaselineLenRef.current = messagesLenRef.current;
      setPillState({ visible: false, hasNew: false, newCount: 0 });
      withProgrammaticScroll(() => c.scrollTo({ top: c.scrollHeight, behavior }), behavior);
      armSettleTimers();
    },
    [getScrollContainer, withProgrammaticScroll, armSettleTimers, setPillState],
  );

  // Re-apply the pin target; called by the ResizeObserver each time content
  // settles, so async media finishing layout can't strand the user mid-thread
  // ('bottom') or clamp a remembered offset short ('offset'). Applied right
  // there, not in a deferred frame: the observer already runs after layout and
  // before paint, so the frame that shows the taller transcript is the frame
  // that shows it scrolled. Deferring by a frame painted the growth first and
  // the scroll a frame later, a visible snap on every reload and return.
  const reapplyPin = useCallback(() => {
    const c = getScrollContainer(scrollAreaRef);
    const target = pinTargetRef.current;
    if (!target || !c) return;
    const top =
      target.mode === 'bottom' ? c.scrollHeight
        : target.mode === 'offset' ? target.top
          : target.mode === 'reveal' ? revealTop(c, target.id)
            : anchorTop(c, target.id, target.part);
    if (top == null) {
      // The anchored bubble left the transcript (edit / regenerate truncation).
      pinTargetRef.current = null;
      clearSettleTimers();
      return;
    }
    withProgrammaticScroll(() => c.scrollTo({ top }), 'auto');
    armSettleTimers();
  }, [getScrollContainer, withProgrammaticScroll, armSettleTimers, clearSettleTimers]);

  // Scroll a bubble under the viewport top and hold it there through the settle
  // window. Anything short of the newest turn hands the user the jump pill and
  // a false near-bottom, so a streaming follow can't yank them back down.
  const pinToMessage = useCallback(
    (id: string, behavior: 'auto' | 'smooth' = 'auto', isLatest = false, part?: AnchorPart) => {
      const c = getScrollContainer(scrollAreaRef);
      if (!c) return;
      const top = anchorTop(c, id, part);
      if (top == null) return;
      pinTargetRef.current = { mode: 'anchor', id, part };
      // A request past the maximum clamps, so any turn near enough to the end
      // reads as "at the bottom" by position alone. Only the newest one really
      // is: under an earlier turn the transcript still has room to grow, and
      // calling that the bottom re-arms the follow that carries the reader off
      // the turn they picked.
      const landsAtBottom = isLatest && top >= Math.max(0, c.scrollHeight - c.clientHeight) - 1;
      isNearBottomRef.current = landsAtBottom;
      pillBaselineLenRef.current = messagesLenRef.current;
      setPillState({ visible: !landsAtBottom, hasNew: false, newCount: 0 });
      withProgrammaticScroll(() => c.scrollTo({ top, behavior }), behavior);
      armSettleTimers();
    },
    [getScrollContainer, withProgrammaticScroll, armSettleTimers, setPillState],
  );

  // Bring a turn's deliverables deck into view as it unfolds. The deck cannot
  // do this for itself: its height animates over 260ms, and the observer below
  // re-applies this controller's pin on every one of those growth frames, so a
  // scroll the deck set would be overwritten before it painted. Expressed as a
  // pin target instead, the unfold is followed by the very machinery that was
  // overwriting it, and the settle window releases it once growth stops.
  const revealFiles = useCallback(
    (id: string) => {
      const c = getScrollContainer(scrollAreaRef);
      if (!c) return;
      const top = revealTop(c, id);
      if (top == null) return;
      pinTargetRef.current = { mode: 'reveal', id };
      withProgrammaticScroll(() => c.scrollTo({ top }), 'auto');
      armSettleTimers();
    },
    [getScrollContainer, withProgrammaticScroll, armSettleTimers],
  );

  // Scroll listener + settle-aware ResizeObserver.
  // Re-attaches when activeAgentId changes (ScrollArea remounts on tab switch).
  useEffect(() => {
    const isMain = activeAgentId === 'main';
    const ref = isMain ? scrollAreaRef : subagentScrollAreaRef;
    const nearBottomRef = isMain ? isNearBottomRef : isSubagentNearBottomRef;
    const c = getScrollContainer(ref);
    if (!c) return;

    // Reset to near-bottom when switching tabs
    nearBottomRef.current = true;

    let lastTop = c.scrollTop;
    const handleScroll = () => {
      // The band is how a *user* scroll re-joins the stream. A pin that chose a
      // position must not get to answer it: pinToMessage already decided whether
      // that landing is the bottom, knowing the one thing a position cannot tell
      // it, which turn is the newest. A request past the maximum clamps, so a
      // landing on an earlier turn near the end sits exactly at the maximum and
      // reads as the bottom by any positional test. Letting it re-arm the follow
      // is what walks the reader off the turn they picked once the settle window
      // lets go.
      //
      // A reveal is the same promise about a smaller move. Opening a deck under
      // the streaming turn scrolls just far enough to show the cards, which on a
      // short unfold lands inside the band, and the reader who had paused the
      // follow was counted as rejoining it. Nothing scrolls again after that, so
      // the answer went stale on the ref and the hard cap released the pin into
      // a jump to the bottom, seconds after the click and with no cause on
      // screen.
      const pinMode = pinTargetRef.current?.mode;
      const pinOwnsPosition = programmaticScrollRef.current && (pinMode === 'anchor' || pinMode === 'reveal');
      if (!pinOwnsPosition) {
        const metrics = { scrollTop: c.scrollTop, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight };
        // The band answers a downward scroll. An upward one is a reader
        // leaving, by wheel, key, drag or touch, and only the bottom itself
        // re-arms: inside the band the follow would put them back on the next
        // growth frame, and each frame grows, so a notch at a time they could
        // never get out. A fold that clamps scrollTop moves up too, but lands
        // on the bottom, so it keeps following.
        const movedUp = c.scrollTop < lastTop;
        nearBottomRef.current = isNearBottom(metrics, movedUp ? AT_BOTTOM_PX : NEAR_BOTTOM_PX);
      }
      lastTop = c.scrollTop;
      if (!isMain) return;
      // Record every settle (user scrolls AND pins/follows) so the cross-unmount
      // store always reflects where the transcript actually is — a bottom pin
      // after send must overwrite a stale mid-thread offset. 'bottom' is sticky:
      // re-entry pins to the (possibly taller) new bottom. Offset sessions are
      // the exception: their intermediate scrolls clamp against still-short
      // content and would overwrite the very offset being restored.
      // The band, not the upward rule: a nudge that pauses the follow is not a
      // place worth coming back to, and a numeric save re-opens with the pill.
      if (memoryTidRef.current && pinTargetRef.current?.mode !== 'offset') {
        scrollMemory.set(
          `thread:${memoryTidRef.current}`,
          isNearBottom({ scrollTop: c.scrollTop, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight }, NEAR_BOTTOM_PX) ? 'bottom' : c.scrollTop,
        );
      }
      if (programmaticScrollRef.current) return; // ignore our own scrolls
      if (followTopRef.current != null && Math.abs(c.scrollTop - followTopRef.current) < 1) {
        followTopRef.current = null;
        return;
      }
      followTopRef.current = null;
      // A genuine user scroll takes control away from the pin controller.
      pinTargetRef.current = null;
      clearSettleTimers();
      // Update jump-to-latest pill.
      const atBottom = nearBottomRef.current;
      setJumpPill((prev) => {
        if (atBottom) {
          return prev.visible || prev.hasNew ? { visible: false, hasNew: false, newCount: 0 } : prev;
        }
        if (prev.visible) return prev; // keep hasNew/newCount once shown
        pillBaselineLenRef.current = messagesLenRef.current;
        return { visible: true, hasNew: false, newCount: 0 };
      });
    };
    c.addEventListener('scroll', handleScroll, { passive: true });

    // A real user gesture (wheel / touch) reclaims scroll control even mid
    // programmatic smooth-scroll. Without this, those scroll events are flagged
    // programmatic and ignored above, so the pin keeps yanking against the user.
    // A disclosure toggle: hold the toggled row at the viewport position it had
    // when clicked, for as long as its animation resizes the transcript. The
    // bottom pin is released too, since the reader has just chosen a place.
    let toggleAnchor: { el: HTMLElement; top: number; until: number } | null = null;
    const handleUserIntent = () => {
      if (!isMain) return;
      programmaticScrollRef.current = false;
      pinTargetRef.current = null;
      toggleAnchor = null;
      clearSettleTimers();
      // Also cancel a pending entry-restore frame — the user has taken over.
      if (entryRestoreRafRef.current != null) {
        cancelAnimationFrame(entryRestoreRafRef.current);
        entryRestoreRafRef.current = null;
      }
    };
    c.addEventListener('wheel', handleUserIntent, { passive: true });
    c.addEventListener('touchstart', handleUserIntent, { passive: true });

    const handleAnchoredToggle = (e: Event) => {
      if (!isMain) return;
      const el = e.target as HTMLElement | null;
      if (!el) return;
      toggleAnchor = { el, top: el.getBoundingClientRect().top, until: performance.now() + TOGGLE_HOLD_MS };
      pinTargetRef.current = null;
      clearSettleTimers();
    };    c.addEventListener(ANCHORED_TOGGLE_EVENT, handleAnchoredToggle);

    // Content growth, observed after layout and before paint. While a pin
    // target is set, re-apply it (charts/code/images finishing layout, the fix
    // for landing mid-thread). Otherwise this is the streaming follow: a reader
    // near the bottom is kept there in the same frame the transcript grows.
    // Instant, not smooth: growth per frame is a few px, so an instant set is
    // continuous, whereas a smooth series re-targeted from every growth trails
    // the bottom by hundreds of px under fast streaming and slides when a row
    // folds. Only growth is followed: the observer also fires once on attach
    // and on every shrink, and a follow there would jump a reader whose
    // position a tab return is about to restore, or whom a fold just left
    // exactly where they were.
    let ro: ResizeObserver | null = null;
    if (isMain) {
      let lastHeight = -1;
      ro = new ResizeObserver((entries) => {
        const height = entries[0]?.contentRect.height ?? lastHeight;
        const grew = lastHeight >= 0 && height > lastHeight;
        const growth = grew ? height - lastHeight : 0;
        lastHeight = height;
        if (toggleAnchor) {
          if (performance.now() > toggleAnchor.until || !c.contains(toggleAnchor.el)) {
            toggleAnchor = null;
            // An unfold grows below the held row, so holding it never scrolls
            // and the band is never asked again: the reader who opened a row to
            // read it still counted as following and was carried to the bottom
            // on the next growth. Ask where the hold actually left them, before
            // the growth this callback is reporting, or a reader at the end is
            // read as that growth short of it.
            nearBottomRef.current = isNearBottom(
              { scrollTop: c.scrollTop, scrollHeight: c.scrollHeight - growth, clientHeight: c.clientHeight },
              AT_BOTTOM_PX,
            );
          } else {
            const delta = toggleAnchor.el.getBoundingClientRect().top - toggleAnchor.top;
            if (Math.abs(delta) >= 1) withProgrammaticScroll(() => c.scrollTo({ top: c.scrollTop + delta }), 'auto');
            return;
          }
        }
        if (pinTargetRef.current) {
          reapplyPin();
          return;
        }
        if (!grew || !isFollowing()) return;
        const top = c.scrollHeight - c.clientHeight;
        if (top - c.scrollTop <= 0) return;
        followTopRef.current = top;
        c.scrollTo({ top });
      });
      ro.observe(getScrollContent(c));
    }
    // A hidden tab gets no rendering updates, so the observer above does not
    // fire and the view sits still while the transcript grows; the animations
    // the tab queued apply their final layout only on return. Close whatever
    // gap that leaves in one instant jump, only for a reader who was following:
    // a user who scrolled up keeps their place. The jump is made twice: at the
    // event, and again inside the first frame, after those animations have
    // applied (see lib/hiddenTabMotion) but before that frame paints.
    const handleVisibility = () => {
      if (document.visibilityState !== 'visible' || !isMain) return;
      const jump = () => {
        if (!isFollowing()) return;
        if (c.scrollHeight - c.scrollTop - c.clientHeight <= 1) return;
        withProgrammaticScroll(() => c.scrollTo({ top: c.scrollHeight }), 'auto');
      };
      jump();
      if (visibilityRafRef.current != null) cancelAnimationFrame(visibilityRafRef.current);
      visibilityRafRef.current = requestAnimationFrame(() => {
        visibilityRafRef.current = null;
        jump();
      });
    };
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      c.removeEventListener('scroll', handleScroll);
      c.removeEventListener('wheel', handleUserIntent);
      c.removeEventListener('touchstart', handleUserIntent);
      c.removeEventListener(ANCHORED_TOGGLE_EVENT, handleAnchoredToggle);
      document.removeEventListener('visibilitychange', handleVisibility);
      if (visibilityRafRef.current != null) {
        cancelAnimationFrame(visibilityRafRef.current);
        visibilityRafRef.current = null;
      }
      ro?.disconnect();
    };
  }, [activeAgentId, getScrollContainer, getScrollContent, reapplyPin, clearSettleTimers, withProgrammaticScroll, isFollowing]);

  // New messages for a reader who is not following: the ResizeObserver above
  // keeps a following reader at the bottom, so all that is left here is the
  // "N new" count on the jump pill. Held until the thread-entry decision
  // (below) has landed, as messages render while history is still hydrating.
  useEffect(() => {
    if (pinTargetRef.current) return; // pin controller owns scroll during settle
    if (!entryRestoreSettled()) return;
    if (isNearBottomRef.current) return;
    const delta = messagesLenRef.current - pillBaselineLenRef.current;
    if (delta > 0) {
      setJumpPill((prev) => (prev.visible ? { visible: true, hasNew: true, newCount: delta } : prev));
    }
  }, [messages, entryRestoreSettled]);

  // Thread-entry restore — the core fix. Fires on the real "history is present"
  // signal (isLoadingHistory flips false), not on an empty/partial list. A
  // remembered mid-thread offset (scrollMemory, survives route unmounts) wins
  // over the default bottom pin, so tabbing away and back lands where the user
  // left; 'bottom' / no memory pins to bottom through the async settle window.
  // A layout effect, applied in the same commit that renders the history: the
  // first frame the user sees of the thread is already in position. The
  // deferred frame remains only for a viewport that is not mounted yet.
  useLayoutEffect(() => {
    if (!isActive) return;
    const tid = currentThreadId || threadId;
    if (!tid || tid === '__default__') return;
    if (isLoadingHistory) return;
    if (restoredForThreadRef.current === tid) return;
    restoredForThreadRef.current = tid;
    const saved = scrollMemory.get(`thread:${tid}`);
    if (typeof saved === 'number') {
      // Async content (charts, markdown, images) keeps growing the transcript
      // after the history signal, so a one-shot scrollTop set clamps short.
      // Run an offset pin session: the ResizeObserver re-applies the target on
      // every growth until the settle window closes — the same machinery that
      // makes land-at-bottom reliable. The claim is synchronous so a
      // message-triggered bottom follow can't slip in before the deferred
      // apply.
      //
      // A numeric save is by construction mid-thread (near-bottom saves record
      // 'bottom'), so reflect that immediately: streaming follow / new-message
      // autoscroll must not yank to the bottom, and the jump-to-latest
      // affordance surfaces without waiting for a user scroll (handleScroll,
      // its usual trigger, never fires here).
      pinTargetRef.current = { mode: 'offset', top: saved };
      isNearBottomRef.current = false;
      pillBaselineLenRef.current = messagesLenRef.current;
      setPillState({ visible: true, hasNew: false, newCount: 0 });
    }
    // One apply for both targets: run in this commit when the viewport is
    // already mounted, on the next frame when it is not.
    const apply = () => {
      // The instance may have gone inactive (cached/hidden) before the frame.
      const c = isActiveRef.current ? getScrollContainer(scrollAreaRef) : null;
      if (!c) {
        // Nothing was applied: release the claim so a stale pin can't block
        // follows when the instance reactivates.
        if (pinTargetRef.current?.mode === 'offset') pinTargetRef.current = null;
        return;
      }
      if (typeof saved === 'number') {
        withProgrammaticScroll(() => {
          c.scrollTop = saved;
        });
        armSettleTimers();
      } else {
        pinToBottom('auto');
      }
    };
    if (getScrollContainer(scrollAreaRef)) {
      apply();
    } else {
      entryRestoreRafRef.current = requestAnimationFrame(() => {
        entryRestoreRafRef.current = null;
        apply();
      });
    }
    return () => {
      if (entryRestoreRafRef.current != null) {
        cancelAnimationFrame(entryRestoreRafRef.current);
        entryRestoreRafRef.current = null;
        // A cancelled frame leaves an offset claim unapplied — release it.
        if (pinTargetRef.current?.mode === 'offset') pinTargetRef.current = null;
      }
    };
  }, [isActive, isLoadingHistory, currentThreadId, threadId, pinToBottom, isActiveRef, getScrollContainer, withProgrammaticScroll, armSettleTimers, setPillState]);

  // Cleanup pending scroll timers/rAF on unmount.
  useEffect(() => {
    return () => {
      if (settleQuietTimerRef.current) clearTimeout(settleQuietTimerRef.current);
      if (settleHardCapRef.current) clearTimeout(settleHardCapRef.current);
      if (entryRestoreRafRef.current != null) cancelAnimationFrame(entryRestoreRafRef.current);
    };
  }, []);

  return {
    scrollAreaRef,
    subagentScrollAreaRef,
    getScrollContainer,
    withProgrammaticScroll,
    pinToBottom,
    pinToMessage,
    revealFiles,
    pinTargetRef,
    saveScrollPosition,
    jumpPill,
    scrollPositionsRef,
    skipSubagentAutoScrollRef,
    activeAgentIdRef,
    isNearBottomRef,
    isSubagentNearBottomRef,
    restoredForThreadRef,
    entryRestoreSettled,
  };
}
