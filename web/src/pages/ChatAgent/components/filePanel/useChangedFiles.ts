import { useCallback, useEffect, useRef, useState } from 'react';
import type { WriteEvent } from '../../utils/fileRefResolver';

const NO_WRITES: WriteEvent[] = [];

/** The newest write of one path in a newest-first log, or null for none. */
const newestWriteOf = (log: readonly WriteEvent[], path: string) => log.find((w) => w.path === path)?.id ?? null;

/**
 * Which open files the agent has rewritten since the tab showing them last
 * read its bytes: the amber dot on a tab.
 *
 * The signal is the thread's own Write/Edit log, newest first, with every
 * write in it. A tab records the id of its file's newest write when it reads
 * the bytes; a different id at the front later is a write that happened
 * since. The id, not a count: the log a lookup uses names each file once, so
 * the second write of a file that was already newest changes nothing in it.
 * A panel with no log (a share, the gallery) simply never marks anything,
 * which is correct: nothing is writing.
 *
 * The log is a live array behind a getter, and nothing re-renders when the
 * agent appends to it, so it is read after each render rather than
 * subscribed to. The equality guard is what keeps that from looping; in
 * practice the render that notices is the one the refreshed file list causes.
 */
export function useChangedFiles(getWriteLog?: (() => WriteEvent[]) | null) {
  const [writes, setWrites] = useState<WriteEvent[]>(NO_WRITES);
  const marks = useRef(new Map<string, string | null>());
  // A mark is stamped from an effect once a read settles, after the render
  // that showed the bytes; the dot it clears needs a render of its own.
  const [, rerender] = useState(0);

  // Deliberately dep-less: it runs after every render, and the equality guard
  // below is what stops the update chain.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const next = getWriteLog?.() ?? NO_WRITES;
    setWrites((prev) => (prev.length === next.length && prev[0]?.id === next[0]?.id ? prev : next.slice()));
  });

  /** This path's bytes are now in hand; writes after this moment are changes. */
  const markRead = useCallback((path: string) => {
    const mark = newestWriteOf(getWriteLog?.() ?? NO_WRITES, path);
    if (marks.current.has(path) && marks.current.get(path) === mark) return;
    marks.current.set(path, mark);
    rerender((n) => n + 1);
  }, [getWriteLog]);

  const forget = useCallback((path: string) => {
    marks.current.delete(path);
  }, []);

  const hasChanged = useCallback((path: string) => {
    const mark = marks.current.get(path);
    if (mark === undefined) return false;
    const newest = newestWriteOf(writes, path);
    // A write that has scrolled off the capped log is unknowable, not a change.
    return newest !== null && newest !== mark;
  }, [writes]);

  return { markRead, forget, hasChanged };
}
