/**
 * `useAllModels().isLoading` gates the composer's model picker, so it has to
 * cover every input to "which models can this account reach". The platform
 * access answer is one of them: while it is pending, a locked model reads as
 * reachable and a pick would save it as the account default.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';

const h = vi.hoisted(() => ({ platformLoading: false }));

vi.mock('../useModels', () => ({ useModels: () => ({ models: { models: {} }, isLoading: false }) }));
vi.mock('../usePreferences', () => ({ usePreferences: () => ({ preferences: {}, isLoading: false }) }));
vi.mock('../useConfiguredProviders', () => ({
  useConfiguredProviders: () => ({ providers: [], isLoading: false }),
}));
vi.mock('../usePlatformModels', () => ({
  usePlatformModels: () => ({ platform: null, isLoading: h.platformLoading }),
  useModelAccessMap: () => ({}),
}));

import { useAllModels } from '../useAllModels';

describe('useAllModels — isLoading', () => {
  beforeEach(() => { h.platformLoading = false; });

  it('stays loading while only the platform access answer is pending', () => {
    h.platformLoading = true;
    const { result } = renderHook(() => useAllModels());
    expect(result.current.isLoading).toBe(true);
  });

  it('settles once every input has answered', () => {
    const { result } = renderHook(() => useAllModels());
    expect(result.current.isLoading).toBe(false);
  });
});
