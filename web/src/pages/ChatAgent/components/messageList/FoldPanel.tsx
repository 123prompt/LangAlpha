import React, { useLayoutEffect, useRef, useState } from 'react';
import { SPRING_FOLD } from './liveZoneTiming';
import { animate, useReducedMotion, type AnimationPlaybackControls } from 'framer-motion';

interface FoldPanelProps {
  open: boolean;
  children: React.ReactNode;
}

type Phase = 'closed' | 'opening' | 'open' | 'closing';

/**
 * A block of the transcript that folds shut and back open, in px.
 *
 * Every keyframe is a number measured from the content: framer cannot tween
 * from `auto`, and resolving an `auto` keyframe inside the chat scroller jumps
 * the box to its target for one forced layout, where Chromium clamps the
 * scroll offset to the shorter transcript and keeps the clamp. Once open the
 * box hands its height back to the content (`auto`), so a reasoning row or
 * accordion opening inside it is never clipped; a close first freezes the
 * current px, then tweens down. Content that grows while the panel is opening
 * (an accordion unfolding beneath it) retargets the running spring rather than
 * waiting for it.
 *
 * Children are mounted only while the panel is open or moving: a long thread
 * keeps dozens of folded turns, and their process should cost nothing.
 *
 * The list spaces its blocks with a sibling margin that vanishes the moment a
 * panel is hidden, and lands again the moment it is not. That step is folded
 * into the motion: a negative bottom margin cancels the neighbour's gap in
 * proportion to how far the panel is closed, so the last frame of a close and
 * the first of an open move nothing.
 */
function gapBelow(outer: HTMLElement): number {
  const next = outer.nextElementSibling;
  if (!(next instanceof HTMLElement)) return 0;
  const gap = parseFloat(getComputedStyle(next).marginTop);
  return Number.isFinite(gap) ? gap : 0;
}
export function FoldPanel({ open, children }: FoldPanelProps): React.ReactElement {
  const outerRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const [mounted, setMounted] = useState(open);
  const phaseRef = useRef<Phase>(open ? 'open' : 'closed');
  const heightRef = useRef(0);
  const controlsRef = useRef<AnimationPlaybackControls | null>(null);
  const reduceMotion = useReducedMotion();
  const reduceRef = useRef(reduceMotion);
  reduceRef.current = reduceMotion;
  // A close tweens down what was there. The same commit that closes the
  // panel re-renders its blocks for the folded turn (an accordion told to
  // shut), and a body collapsing under a shrinking clip reads as two motions.
  // The children of the last open render are kept until the panel is gone.
  const shownRef = useRef(children);
  if (open) shownRef.current = children;

  useLayoutEffect(() => {
    const outer = outerRef.current;
    if (!outer) return;
    const phase = phaseRef.current;

    if (open) {
      if (!mounted) { setMounted(true); return; }
      if (phase === 'open' || phase === 'opening') return;
      const inner = innerRef.current;
      if (!inner) return;
      // Declared before `finish` so the reduced-motion path, which finishes
      // before there is anything to observe, still reads a bound name.
      let ro: ResizeObserver | null = null;
      const finish = () => {
        controlsRef.current = null;
        phaseRef.current = 'open';
        outer.style.height = 'auto';
        outer.style.marginBottom = '';
        // The panel is its own size again, so nothing is left to follow. The
        // effect's teardown only runs on the next open or close, which would
        // otherwise leave one idle observer per expanded turn for the session.
        ro?.disconnect();
        ro = null;
      };
      // Stop a running close before any write: stopping ticks it one last
      // time, and that tick writes the close's faded opacity and height. An
      // opacity set before the stop was overwritten by it, and the panel
      // reopened to full height at a tenth of its ink until the next close.
      controlsRef.current?.stop();
      controlsRef.current = null;
      if (reduceRef.current) { outer.style.opacity = '1'; finish(); return; }
      phaseRef.current = 'opening';
      outer.style.opacity = '1';
      // The neighbour's gap landed with this commit; cancel it in the same
      // layout, before the first frame paints, not from the spring's first
      // update a frame later.
      const gap = gapBelow(outer);
      if (gap) outer.style.marginBottom = `${-gap}px`;
      const run = (target: number) => {
        controlsRef.current?.stop();
        controlsRef.current = animate(heightRef.current, target, {
          ...SPRING_FOLD,
          onUpdate: (v) => {
            heightRef.current = v;
            outer.style.height = `${v}px`;
            outer.style.marginBottom = gap && target > 0 ? `${-gap * Math.max(0, 1 - v / target)}px` : '';
          },
          onComplete: finish,
        });
      };
      run(inner.getBoundingClientRect().height);
      ro = new ResizeObserver(() => {
        if (phaseRef.current !== 'opening' || !innerRef.current) return;
        run(innerRef.current.getBoundingClientRect().height);
      });
      ro.observe(inner);
      return () => { ro?.disconnect(); ro = null; };
    }

    if (phase === 'closed' || phase === 'closing') return;
    // The gap compensation stays on the box until the commit that hides it.
    // Clearing it here, a frame before React sets `hidden`, grew the list by
    // one gap for that frame and the whole transcript jumped down and back.
    // Hidden, the margin is inert, and the next open writes its own.
    const finish = () => {
      controlsRef.current = null;
      phaseRef.current = 'closed';
      heightRef.current = 0;
      setMounted(false);
    };
    if (reduceRef.current) { finish(); return; }
    // Freeze the open box at its px height before the tween, never from auto.
    // Only reads happen here: a turn folds every panel in one commit, and a
    // write between two panels' reads would force a layout for each. The
    // first write lands with the spring's first frame.
    // Fractional, not offsetHeight: a row sits on a half pixel, and eight
    // panels each rounded up moved the transcript 2px on the first frame.
    // A close that lands mid-open stops the spring first, for the same
    // reason: its last tick would move the box after the height was read.
    controlsRef.current?.stop();
    controlsRef.current = null;
    heightRef.current = outer.getBoundingClientRect().height;
    const full = heightRef.current;
    const gap = gapBelow(outer);
    phaseRef.current = 'closing';
    // A close is an ease, not the open's spring, and a tall process takes a
    // longer beat than a short one: the spring front-loads its travel, and
    // three screens of process crossing in its first 150ms read as a cut,
    // with the answer arriving from nowhere. Bounded so nothing drags.
    const duration = Math.min(0.7, 0.3 + full / 6000);
    controlsRef.current = animate(full, 0, {
      type: 'tween',
      duration,
      ease: [0.45, 0, 0.2, 1],
      onUpdate: (v) => {
        heightRef.current = v;
        // The content is clipped from the bottom as the box comes down, and
        // dims with what is left of it. A fade that ran ahead of the box
        // emptied a tall process in a blink and left a blank hole shrinking
        // for the rest of the spring, which read as the transcript flashing.
        outer.style.opacity = full > 0 ? String(Math.max(0, v / full)) : '0';
        outer.style.height = `${v}px`;
        outer.style.marginBottom = gap && full > 0 ? `${-gap * Math.max(0, 1 - v / full)}px` : '';
      },
      onComplete: finish,
    });
  }, [open, mounted]);

  useLayoutEffect(() => () => { controlsRef.current?.stop(); }, []);

  const settledClosed = !open && !mounted;
  return (
    <div
      ref={outerRef}
      hidden={settledClosed}
      style={{
        overflow: 'hidden',
        height: settledClosed ? 0 : phaseRef.current === 'open' ? 'auto' : `${heightRef.current}px`,
        opacity: phaseRef.current === 'open' ? 1 : undefined,
      }}
    >
      {/* flow-root: the spring's target is this box, and it has to contain a
          child's margin rather than let it collapse through, or the box hands
          back to `auto` a few px taller than the spring ever reached. */}
      <div ref={innerRef} style={{ display: 'flow-root' }}>{mounted ? shownRef.current : null}</div>
    </div>
  );
}
