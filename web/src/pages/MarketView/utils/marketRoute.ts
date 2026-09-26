/**
 * The spelling of a MarketView URL. `buildMarketViewUrl` writes it for the
 * chat's chart tab, the annotation card and the dashboard header;
 * `readMarketViewRoute` reads it back for `MarketView.tsx` and
 * `MarketChatPanel.tsx`, so the two sides share one set of param names.
 * Older Dashboard call sites still spell `/market?symbol=` by hand.
 */
export interface MarketViewRoute {
  symbol: string;
  timeframe?: string;
  /** The PTC workspace whose chat should continue beside the chart. Naming one
   *  also selects PTC mode, since that mode is what the workspace belongs to. */
  workspaceId?: string | null;
  threadId?: string | null;
  /** Where "Return to chat" goes. */
  returnTo?: string | null;
}

export type MarketViewMode = 'ptc' | 'fast';

/** What a MarketView URL carries, each field null when the param is absent. */
export interface MarketViewRouteParams {
  symbol: string | null;
  timeframe: string | null;
  workspaceId: string | null;
  mode: MarketViewMode | null;
  threadId: string | null;
  returnTo: string | null;
}

/** The thread id that means "no particular thread"; the URL never carries it. */
export const DEFAULT_MARKET_THREAD = '__default__';

const PARAM = {
  symbol: 'symbol',
  timeframe: 'tf',
  mode: 'mode',
  workspaceId: 'ws',
  threadId: 'thread',
  returnTo: 'returnTo',
} as const;

/** Every param the route owns, for a reader that clears them once consumed. */
export const MARKET_VIEW_ROUTE_PARAMS: readonly string[] = Object.values(PARAM);

export function buildMarketViewUrl({ symbol, timeframe, workspaceId, threadId, returnTo }: MarketViewRoute): string {
  const sp = new URLSearchParams();
  sp.set(PARAM.symbol, symbol);
  if (timeframe) sp.set(PARAM.timeframe, timeframe);
  if (workspaceId) {
    sp.set(PARAM.mode, 'ptc');
    sp.set(PARAM.workspaceId, workspaceId);
  }
  if (threadId && threadId !== DEFAULT_MARKET_THREAD) sp.set(PARAM.threadId, threadId);
  if (returnTo) sp.set(PARAM.returnTo, returnTo);
  return `/market?${sp.toString()}`;
}

export function readMarketViewRoute(searchParams: URLSearchParams): MarketViewRouteParams {
  const modeParam = searchParams.get(PARAM.mode);
  return {
    symbol: searchParams.get(PARAM.symbol),
    timeframe: searchParams.get(PARAM.timeframe),
    workspaceId: searchParams.get(PARAM.workspaceId),
    mode: modeParam === 'ptc' || modeParam === 'fast' ? modeParam : null,
    threadId: searchParams.get(PARAM.threadId),
    returnTo: searchParams.get(PARAM.returnTo),
  };
}
