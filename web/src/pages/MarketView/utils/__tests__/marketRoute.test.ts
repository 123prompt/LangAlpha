import { describe, expect, it } from 'vitest';
import { buildMarketViewUrl, DEFAULT_MARKET_THREAD, MARKET_VIEW_ROUTE_PARAMS, readMarketViewRoute } from '../marketRoute';

const paramsOf = (url: string) => new URLSearchParams(url.slice(url.indexOf('?')));

describe('marketRoute', () => {
  it('round-trips every field through the URL', () => {
    const url = buildMarketViewUrl({
      symbol: 'AAPL',
      timeframe: '1hour',
      workspaceId: 'ws-7',
      threadId: 'thread-1',
      returnTo: '/chat/t/thread-1',
    });
    expect(url.startsWith('/market?')).toBe(true);
    expect(readMarketViewRoute(paramsOf(url))).toEqual({
      symbol: 'AAPL',
      timeframe: '1hour',
      workspaceId: 'ws-7',
      mode: 'ptc',
      threadId: 'thread-1',
      returnTo: '/chat/t/thread-1',
    });
  });

  it('selects PTC mode only when a workspace is named', () => {
    const bare = readMarketViewRoute(paramsOf(buildMarketViewUrl({ symbol: 'AAPL' })));
    expect(bare).toEqual({ symbol: 'AAPL', timeframe: null, workspaceId: null, mode: null, threadId: null, returnTo: null });

    const withWs = paramsOf(buildMarketViewUrl({ symbol: 'AAPL', workspaceId: 'ws-7' }));
    expect(withWs.get('mode')).toBe('ptc');
    expect(readMarketViewRoute(withWs).mode).toBe('ptc');
  });

  it('omits the thread param for the default thread', () => {
    const url = buildMarketViewUrl({ symbol: 'AAPL', threadId: DEFAULT_MARKET_THREAD });
    expect(paramsOf(url).has('thread')).toBe(false);
    expect(readMarketViewRoute(paramsOf(url)).threadId).toBeNull();
  });

  it('reads an unknown mode as none', () => {
    expect(readMarketViewRoute(new URLSearchParams('symbol=AAPL&mode=turbo')).mode).toBeNull();
    expect(readMarketViewRoute(new URLSearchParams('symbol=AAPL&mode=fast')).mode).toBe('fast');
  });

  it('lists the params the writer sets, so a reader can clear exactly those', () => {
    const url = buildMarketViewUrl({ symbol: 'AAPL', timeframe: '1day', workspaceId: 'ws', threadId: 't', returnTo: '/x' });
    expect([...paramsOf(url).keys()].sort()).toEqual([...MARKET_VIEW_ROUTE_PARAMS].sort());
  });
});
