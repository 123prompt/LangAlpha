import React, { useContext, useLayoutEffect, useRef } from 'react';
import {
  animate,
  motion,
  PresenceContext,
  useIsPresent,
  useMotionValue,
  useReducedMotion,
  type AnimationPlaybackControls,
} from 'framer-motion';
import { SPRING_SNAPPY, EXIT_TWEEN } from './liveZoneTiming';

/**
 * Enter/exit wrapper for the live-zone rows that never hands framer `auto`.
 *
 * framer cannot tween from `auto`: to resolve the exit keyframes it jumps the
 * row's inline height to 0, forces a layout, measures, then restores. During
 * that one forced layout the transcript is a whole row shorter, Chromium
 * clamps the chat scroll container's scrollTop to the shorter content, and the
 * clamp survives the restore, so the view jumps up at the first frame of every
 * fold of a tall row. framer only guards the window scroll, not an inner
 * scroller. Measuring the content ourselves keeps every keyframe in px.
 *
 * Growth is written to the box at the commit that caused it, from a
 * MutationObserver, which runs in the microtask after React's DOM writes and
 * ahead of the frame's ResizeObserver pass. A height that went through React
 * state and framer's frameloop lands one frame after the content did, and
 * that frame paints the new line clipped to half its glyphs. Writing it from
 * this row's own ResizeObserver instead is also a frame late for the follow:
 * the transcript's observer sits above this one, so a size it is handed from
 * a deeper callback is delivered on the next pass, with a loop error on every
 * growth frame. The ResizeObserver stays for growth that is not a mutation
 * (fonts, images).
 *
 * Only the live zone folds while the follow is pinning the reader to the end,
 * which is where the clamp shows; panels the reader opens and closes by hand
 * elsewhere in the transcript still animate `auto` and are a follow-up.
 */

/** How long a row counts as entering. Its enter spring keeps retargeting to
 *  whatever the content becomes in that window, so a tool row whose label
 *  fills in over its first frames still glides in instead of snapping. */
const ENTER_MS = 500;
/** Half the former `space-y-2` on each side, so row-to-row spacing stays
 *  0.5rem and tracks --app-font-scale; the gap closes with the row's height. */
const ROW_GAP = '0.25rem';

interface LiveRowProps {
  /** Target opacity while present; the row exits to 0 on its own. */
  opacity?: number;
  /** Applied to a content box inside the measured one, so a padding class
   *  here is honoured and counted in the row's height. The outer box is
   *  border-box: padding there would desynchronise it from the measurement. */
  className?: string;
  children: React.ReactNode;
}

export function LiveRow({ opacity = 1, className, children }: LiveRowProps) {
  const outerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const height = useMotionValue(0);
  // The exit owns the height once presence is gone: a settle that touched it
  // then would stop framer's exit animation, whose completion is what
  // AnimatePresence waits on to unmount the row, and the row would stay
  // mounted at opacity 0 for good. Presence is read from the nearest
  // AnimatePresence only: one wrapped around the whole live zone would not
  // reach here (framer propagates an outer exit only on request).
  const isPresent = useIsPresent();
  const presentRef = useRef(isPresent);
  presentRef.current = isPresent;
  const wasPresentRef = useRef(isPresent);
  // A row already there when its AnimatePresence first rendered (a reload
  // mid-run, a tab return) opens settled, as `initial={false}` asks.
  const skipEnterRef = useRef(useContext(PresenceContext)?.initial === false);
  const reduceMotion = useReducedMotion();
  const reduceRef = useRef(reduceMotion);
  reduceRef.current = reduceMotion;
  const settleRef = useRef<(reenter?: boolean) => void>(() => {});

  useLayoutEffect(() => {
    const outer = outerRef.current;
    const inner = innerRef.current;
    if (!outer || !inner) return;
    let mountedAt = skipEnterRef.current ? -Infinity : performance.now();
    let target = 0;
    let running: AnimationPlaybackControls | null = null;
    const settle = (reenter = false) => {
      if (!presentRef.current) return;
      // No boxes at all: the view holding this row is display:none (a cached
      // background thread that keeps streaming). Its content is still there;
      // measuring it as 0 would fold every row and clamp the scroll on return.
      if (inner.getClientRects().length === 0) return;
      const next = inner.getBoundingClientRect().height;
      if (next === target && !reenter) return;
      const shrink = next < target;
      target = next;
      running?.stop();
      running = null;
      // A re-entry mid-exit starts from wherever the exit tween stopped, so
      // it unfolds like an entry rather than popping open.
      if (reenter) mountedAt = performance.now();
      // Entry unfolds and a shrink is a state change (a body collapsing to
      // its title, a result replacing a spinner): both animate. Growth is
      // streaming text and lands in the frame the content did.
      if (!reduceRef.current && (shrink || performance.now() - mountedAt < ENTER_MS)) {
        running = animate(height, next, SPRING_SNAPPY);
        return;
      }
      // Both writes are needed: the style write is what paints this frame
      // (framer applies a jumped value on its own next frame), and the jump
      // is what stops that frame from writing the old value back.
      height.jump(next);
      outer.style.height = `${next}px`;
    };
    settleRef.current = settle;
    settle();
    const mo = new MutationObserver(() => settle());
    mo.observe(inner, { childList: true, characterData: true, subtree: true });
    const ro = new ResizeObserver(() => settle());
    ro.observe(inner);
    return () => {
      mo.disconnect();
      ro.disconnect();
      running?.stop();
    };
  }, [height]);

  // A row that re-enters mid-exit keeps whatever height the exit had reached:
  // framer stops the tween where it is and has no px target to return to.
  // Re-measure and reopen it.
  useLayoutEffect(() => {
    if (isPresent && !wasPresentRef.current) settleRef.current(true);
    wasPresentRef.current = isPresent;
  }, [isPresent]);

  return (
    <motion.div
      ref={outerRef}
      initial={{ opacity: 0 }}
      animate={{ opacity }}
      exit={{ opacity: 0, height: 0, transition: reduceMotion ? { duration: 0 } : EXIT_TWEEN }}
      transition={SPRING_SNAPPY}
      style={{ height, overflow: 'hidden' }}
    >
      <div ref={innerRef} style={{ paddingTop: ROW_GAP, paddingBottom: ROW_GAP }}>
        <div className={className}>{children}</div>
      </div>
    </motion.div>
  );
}
