/**
 * Marks the mounted chat view's thread as ACTIVE in the lifecycle store
 * (active thread never shows the unseen dot) and stamps the durable seen
 * cursor on open when the thread's effective observation is already terminal
 * — the "user opened the finished thread" transition.
 *
 * Settles that happen WHILE the thread is open are stamped by the feed
 * client (run_settled on the active thread) and by the local stream's
 * natural-end path in useChatMessages; this hook owns only the on-open case.
 */
import { useEffect } from 'react';
import { markThreadSeen } from './seen';
import {
  getEffectiveObservation,
  setActiveThread,
  TERMINAL_FAMILY,
} from './store';

export function useActiveThreadPublisher(threadId: string | null | undefined): void {
  useEffect(() => {
    const tid = threadId && threadId !== '__default__' ? threadId : null;
    setActiveThread(tid);
    if (tid) {
      const eff = getEffectiveObservation(tid);
      if (eff?.runId && TERMINAL_FAMILY.has(eff.status)) {
        markThreadSeen(tid, eff.runId);
      }
    }
    return () => setActiveThread(null);
  }, [threadId]);
}
