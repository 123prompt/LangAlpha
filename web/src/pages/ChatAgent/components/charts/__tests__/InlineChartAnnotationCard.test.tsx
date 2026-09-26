import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

// Stub the OHLC fetch hook so the resting card's preview chart has bars without
// hitting React Query / the network. Two ascending bars → an "up" green trend.
vi.mock('@/pages/MarketView/hooks/useStockBars', () => ({
  useStockBars: () => ({
    bars: [
      { time: 1_700_000_000, open: 100, high: 102, low: 99, close: 101 },
      { time: 1_700_086_400, open: 101, high: 110, low: 100, close: 108 },
    ],
    isLoading: false,
    isError: false,
  }),
}));

import { WorkspaceProvider } from '../../../contexts/WorkspaceContext';
import { ChartSurfaceContext, type ChartSurface } from '../../../contexts/ChartSurfaceContext';
import { chartAnnotationStore, makeChartId } from '@/pages/MarketView/stores/chartAnnotationStore';
import { InlineChartAnnotationCard } from '../InlineChartAnnotationCard';
import { MessageActionsProvider } from '../../messageList/MessageActionsContext';

const ARTIFACT = {
  type: 'chart_annotation',
  op: 'add',
  symbol: 'NVDA',
  workspace_id: 'ws-art',
  annotation_id: 'ann_1',
  annotations: [
    { annotation_id: 'ann_1', symbol: 'NVDA', type: 'price_line', price: 205, label: 'Resistance' },
    {
      annotation_id: 'ann_2',
      symbol: 'NVDA',
      type: 'rectangle',
      point1: { time: '2024-10-16T00:00:00Z', price: 150 },
      point2: { time: '2024-11-20T00:00:00Z', price: 140 },
    },
  ],
};

function LocationDisplay(): React.ReactElement {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname + loc.search}</div>;
}

function renderCard(
  artifact: Record<string, unknown>,
  surface: Partial<ChartSurface> = {},
  onOpenChart?: (spec: { symbol: string; timeframe?: string }) => void,
  workspaceId: string | null = 'ws-ctx',
) {
  const value: ChartSurface = { chartPresent: false, ...surface };
  return render(
    <MemoryRouter initialEntries={['/chat/t/thread-123']}>
      <WorkspaceProvider workspaceId={workspaceId} downloadFile={null}>
        <ChartSurfaceContext.Provider value={value}>
          <MessageActionsProvider actions={onOpenChart ? { onOpenChart } : {}}>
            <Routes>
              <Route
                path="/chat/t/:threadId"
                element={<InlineChartAnnotationCard artifact={artifact} />}
              />
              <Route path="/market" element={<LocationDisplay />} />
            </Routes>
          </MessageActionsProvider>
        </ChartSurfaceContext.Provider>
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}

describe('InlineChartAnnotationCard', () => {
  afterEach(() => {
    vi.clearAllMocks();
    chartAnnotationStore._resetForTesting();
  });

  it('renders the spotlight preview card', () => {
    renderCard(ARTIFACT);

    expect(screen.getByText('NVDA')).toBeInTheDocument();
    // The annotation count rides the card's accessible name; the floating legend
    // names the real annotations (labelled price line + the rectangle's "Zone").
    expect(screen.getByRole('button', { name: /2 annotations/ })).toBeInTheDocument();
    expect(screen.getByText('Resistance')).toBeInTheDocument();
    expect(screen.getByText('Open annotated chart')).toBeInTheDocument();
  });

  it('opens the chart tab in the panel, scoped to symbol and timeframe', () => {
    const onOpenChart = vi.fn();
    renderCard({ ...ARTIFACT, timeframe: '1hour' }, {}, onOpenChart);
    fireEvent.click(screen.getByRole('button'));

    // The artifact's workspace rides along: the tab draws that workspace's annotations, not the panel's.
    expect(onOpenChart).toHaveBeenCalledWith({ symbol: 'NVDA', timeframe: '1hour', workspaceId: 'ws-art' });
    // Nothing navigated away: the chart is a tab beside the chat.
    expect(screen.queryByTestId('loc')).not.toBeInTheDocument();
  });

  // Clearing the chart only hides the drawing; asking for it from the card
  // brings it back, as the MarketView chip does.
  it('re-shows a drawing the user cleared from the chart', () => {
    const chartId = makeChartId('NVDA', '1day');
    chartAnnotationStore.clearDisplay('ws-art', chartId);
    renderCard(ARTIFACT, {}, vi.fn());
    fireEvent.click(screen.getByRole('button'));
    expect(chartAnnotationStore.isDisplayCleared('ws-art', chartId)).toBe(false);
  });

  // The spotlight card is a role="button" — it must be keyboard-operable, not
  // just mouse-clickable. Enter and Space both open the chart.
  it.each(['Enter', ' '])('opens the chart via the %s key (keyboard a11y)', (key) => {
    const onOpenChart = vi.fn();
    renderCard(ARTIFACT, {}, onOpenChart);
    const card = screen.getByRole('button');
    expect(card).toHaveAttribute('tabindex', '0');

    fireEvent.keyDown(card, { key });
    expect(onOpenChart).toHaveBeenCalledWith({ symbol: 'NVDA', timeframe: '1day', workspaceId: 'ws-art' });
  });

  // A share mounts the transcript with no workspace, and the chart it opens
  // carries no drawing, so the card offers the chart and promises no more.
  it('offers a plain chart, not an annotated one, on a share', () => {
    const onOpenChart = vi.fn();
    renderCard(ARTIFACT, {}, onOpenChart, null);

    expect(screen.getByText('Open chart')).toBeInTheDocument();
    expect(screen.queryByText('Open annotated chart')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'NVDA 1day, 2 annotations. Open chart.' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button'));
    expect(onOpenChart).toHaveBeenCalledWith({ symbol: 'NVDA', timeframe: '1day', workspaceId: 'ws-art' });
  });

  it('opens the tab on the panel workspace when the artifact names none', () => {
    const onOpenChart = vi.fn();
    const { workspace_id: _omitted, ...bare } = ARTIFACT;
    renderCard(bare, {}, onOpenChart);
    fireEvent.click(screen.getByRole('button'));

    expect(onOpenChart).toHaveBeenCalledWith({ symbol: 'NVDA', timeframe: '1day', workspaceId: 'ws-ctx' });
  });

  it('falls back to MarketView, carrying symbol, ptc mode, workspace, thread, returnTo, when no panel can open it', async () => {
    renderCard(ARTIFACT);
    fireEvent.click(screen.getByRole('button'));

    const loc = await screen.findByTestId('loc');
    const url = loc.textContent || '';
    expect(url.startsWith('/market?')).toBe(true);
    const params = new URLSearchParams(url.slice(url.indexOf('?')));
    expect(params.get('symbol')).toBe('NVDA');
    expect(params.get('mode')).toBe('ptc');
    expect(params.get('ws')).toBe('ws-art');
    expect(params.get('thread')).toBe('thread-123');
    expect(params.get('returnTo')).toBe('/chat/t/thread-123');
  });

  it('uses the artifact timeframe for the bubble and the MarketView URL', async () => {
    renderCard({ ...ARTIFACT, timeframe: '1hour' });

    // The pill shows the short label; the full timeframe rides the accessible name.
    expect(screen.getByText('1H')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /1hour/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button'));
    const loc = await screen.findByTestId('loc');
    const url = loc.textContent || '';
    const params = new URLSearchParams(url.slice(url.indexOf('?')));
    expect(params.get('tf')).toBe('1hour');
  });

  it('collapses to a chip (no chart) when a chart is present', () => {
    renderCard(ARTIFACT, { chartPresent: true });

    expect(screen.getByText(/on chart/i)).toBeInTheDocument();
    expect(screen.queryByText('Open in MarketView')).not.toBeInTheDocument();
    expect(screen.queryByTestId('surface')).not.toBeInTheDocument();
  });

  it('chip restores a cleared drawing to the chart when clicked', () => {
    // The drawing was cleared from the chart elsewhere (the Clear button).
    chartAnnotationStore.clearDisplay('ws-art', 'NVDA:1day');
    renderCard(ARTIFACT, { chartPresent: true });

    // Chip reflects the cleared state and invites re-showing.
    expect(screen.getByText(/show .* on chart/i)).toBeInTheDocument();
    expect(chartAnnotationStore.isDisplayCleared('ws-art', 'NVDA:1day')).toBe(true);

    fireEvent.click(screen.getByRole('button'));
    expect(chartAnnotationStore.isDisplayCleared('ws-art', 'NVDA:1day')).toBe(false);
  });

  it('chip jumps the chart to a different ticker than the one on screen', () => {
    const onJumpToChart = vi.fn();
    // Drawing is on NVDA but the live chart shows AAPL → the chip offers a jump.
    chartAnnotationStore.clearDisplay('ws-art', 'NVDA:1day');
    renderCard(ARTIFACT, {
      chartPresent: true,
      activeSymbol: 'AAPL',
      activeTimeframe: '1day',
      onJumpToChart,
    });

    expect(screen.getByText(/view .* on chart/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button'));
    expect(onJumpToChart).toHaveBeenCalledWith('NVDA', '1day');
    // Jumping also un-clears the instance so it shows once the chart switches.
    expect(chartAnnotationStore.isDisplayCleared('ws-art', 'NVDA:1day')).toBe(false);
  });

  it('chip confirms (no jump) when it already describes the on-screen instance', () => {
    const onJumpToChart = vi.fn();
    renderCard(ARTIFACT, {
      chartPresent: true,
      activeSymbol: 'NVDA',
      activeTimeframe: '1day',
      onJumpToChart,
    });

    expect(screen.getByText(/on chart/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button'));
    expect(onJumpToChart).not.toHaveBeenCalled();
  });
});
