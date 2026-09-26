import { describe, it, expect } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useChangedFiles } from '../useChangedFiles';
import type { WriteEvent } from '../../../utils/fileRefResolver';

describe('useChangedFiles', () => {
  it('marks a file the agent writes again after the tab read it', () => {
    const log = { current: [] as WriteEvent[] };
    const { result, rerender } = renderHook(() => useChangedFiles(() => log.current));

    act(() => result.current.markRead('a.md'));
    expect(result.current.hasChanged('a.md')).toBe(false);

    log.current = [{ id: 'w1', path: 'a.md' }];
    rerender();
    expect(result.current.hasChanged('a.md')).toBe(true);

    // The tab re-reads: the write it saw is the newest, so the dot goes.
    act(() => result.current.markRead('a.md'));
    rerender();
    expect(result.current.hasChanged('a.md')).toBe(false);

    // The same file written twice: the deduped path list would not move, the log does.
    log.current = [{ id: 'w2', path: 'a.md' }, { id: 'w1', path: 'a.md' }];
    rerender();
    expect(result.current.hasChanged('a.md')).toBe(true);
  });

  it('leaves a file alone that was never read, or whose write was another file', () => {
    const log = { current: [{ id: 'w1', path: 'b.md' }] as WriteEvent[] };
    const { result, rerender } = renderHook(() => useChangedFiles(() => log.current));
    act(() => result.current.markRead('a.md'));
    log.current = [{ id: 'w2', path: 'b.md' }, { id: 'w1', path: 'b.md' }];
    rerender();
    expect(result.current.hasChanged('a.md')).toBe(false);
    expect(result.current.hasChanged('b.md')).toBe(false);
  });
});
