/**
 * Publishes the mounted chat view's OWN run-liveness into the lifecycle
 * store's LOCAL layer, so every surface (nav panel, gallery) reacts instantly,
 * ahead of the user feed.
 *
 * Only the body's running→idle transition counts as a settle — cleanup alone
 * (unmount, LRU eviction, the `__default__` → real id flip mid-run) just drops
 * the local observation; the feed layer persists underneath. A natural end
 * while this thread is active also stamps the durable seen cursor (watched
 * finishes never grow a dot).
 *
 * Sibling of useActiveThreadPublisher: that one owns "the user is looking at
 * this thread", this one owns "this tab is running this thread".
 */
import { useEffect, useRef } from 'react';
import { markThreadSeen } from './seen';
import {
  clearLocalObservation,
  getActiveThreadId,
  publishLocalRunning,
  publishLocalSettled,
} from './store';

export function useLocalRunPublisher(
  threadId: string,
  isLoading: boolean,
  runIdRef: { current: string | null },
): void {
  const wasRunningRef = useRef(false);
  useEffect(() => {
    const running = !!threadId && threadId !== '__default__' && isLoading;
    const wasRunning = wasRunningRef.current;
    wasRunningRef.current = running;
    if (running) {
      publishLocalRunning(threadId, runIdRef.current ?? undefined);
      // The effect fires on the isLoading flip, BEFORE the response-header
      // latch fills the ref — so the first publish has no runId, and a
      // runId-less observation can't be superseded by the feed's settle nor
      // matched by a snapshot. Converge here (the latch happens outside
      // React): re-publish once the ref fills, polling only in that window.
      let timer: ReturnType<typeof setTimeout> | undefined;
      if (runIdRef.current == null) {
        const poll = () => {
          if (runIdRef.current != null) {
            publishLocalRunning(threadId, runIdRef.current);
          } else {
            timer = setTimeout(poll, 500);
          }
        };
        timer = setTimeout(poll, 500);
      }
      return () => {
        if (timer !== undefined) clearTimeout(timer);
        clearLocalObservation(threadId);
      };
    }
    if (wasRunning && threadId && threadId !== '__default__') {
      const runId = runIdRef.current ?? undefined;
      publishLocalSettled(threadId, runId);
      if (runId && getActiveThreadId() === threadId) {
        markThreadSeen(threadId, runId);
      }
    }
    // runIdRef is a stable ref container — read at effect time, never a dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoading, threadId]);
}
