import React, { useLayoutEffect, useRef } from 'react';
import { animate, useReducedMotion } from 'framer-motion';
import { EXIT_TWEEN } from './liveZoneTiming';

export function SettlingLabel({ text }: { text: string }): React.ReactElement {
  const outerRef = useRef<HTMLSpanElement>(null);
  const innerRef = useRef<HTMLSpanElement>(null);
  const widthRef = useRef<number | null>(null);
  const reduceMotion = useReducedMotion();
  useLayoutEffect(() => {
    const outer = outerRef.current;
    const inner = innerRef.current;
    if (!outer || !inner) return;
    const next = inner.getBoundingClientRect().width;
    const prev = widthRef.current;
    widthRef.current = next;
    if (prev === null || reduceMotion || Math.abs(next - prev) < 0.5) { outer.style.width = ''; return; }
    // Hold the old width in this same layout, before the frame paints: the
    // tween's first write lands a frame later, and one paint at the new
    // width is the hop this exists to remove.
    outer.style.width = `${prev}px`;
    const controls = animate(prev, next, {
      ...EXIT_TWEEN,
      onUpdate: (v) => { outer.style.width = `${v}px`; },
      onComplete: () => { outer.style.width = ''; },
    });
    return () => { controls.stop(); outer.style.width = ''; };
  }, [text, reduceMotion]);
  return (
    <span ref={outerRef} className="min-w-0 max-w-full overflow-hidden text-ellipsis whitespace-nowrap">
      <span ref={innerRef} className="inline-block">{text}</span>
    </span>
  );
}
