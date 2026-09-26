import React, { useState, useEffect } from 'react';
import { Info, List, ChevronDown } from 'lucide-react';
import { SymbolSwitcher } from './SymbolSwitcher';
import { HeaderPill } from './HeaderPill';
import { ExtendedHoursPair } from './ExtendedHoursPair';
import './StockHeader.css';
import type { StockSearchHit } from '@/lib/marketUtils';
import { compactNumberFixed2, fixed2, signedFixed2 } from '@/lib/format';
import { DASH, fixed2OrDash as fmt, type StockQuoteModel } from '../hooks/useStockQuoteModel';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useTranslation } from 'react-i18next';
import type { ConnectionStatus, DataLevel } from '../hooks/useMarketDataWS';

interface ChartMeta {
  dateRange?: { from: string; to: string };
  dataPoints?: number;
  [key: string]: unknown;
}

interface StockHeaderProps {
  symbol: string;
  /** Derived once by the host (`useStockQuoteModel`), shared with the legend strips. */
  quote: StockQuoteModel;
  chartMeta: ChartMeta | null;
  onToggleOverview: () => void;
  onOpenWatchlist?: () => void;
  wsStatus: ConnectionStatus;
  wsHasData?: boolean;
  wsDataLevel?: DataLevel;
  ginlixDataEnabled?: boolean;
  /** Makes the ticker a control: clicking it opens a search, and a pick lands here. */
  onSwitchSymbol?: (symbol: string, hit?: StockSearchHit) => void;
  /** A host's own buttons, beside Company Overview. */
  headerActions?: React.ReactNode;
}

const EXCHANGE_LABELS: Record<string, string> = { HK: 'HK', SS: 'SH', SZ: 'SZ', L: 'LON', T: 'TYO', TO: 'TSX', AX: 'ASX' };

function getVenueStatusLabel(sym: string | null | undefined, status: string): string {
  if (!sym) return status;
  const dotIdx = sym.lastIndexOf('.');
  if (dotIdx === -1) return status;
  const suffix = sym.slice(dotIdx + 1).toUpperCase();
  return EXCHANGE_LABELS[suffix] ? `${EXCHANGE_LABELS[suffix]} ${status}` : status;
}

const StockHeader = ({ symbol, quote: q, chartMeta: _chartMeta, onToggleOverview, onOpenWatchlist, wsStatus, wsHasData = false, wsDataLevel = null, ginlixDataEnabled: _ginlixDataEnabled = true, onSwitchSymbol, headerActions }: StockHeaderProps) => {
  const { t } = useTranslation();
  const {
    headline, status, tickAt, changePercent,
    previousClose, open, high, low, fiftyTwoWeekHigh, fiftyTwoWeekLow, averageVolume, shownVolume, volumeIsAverage,
    displayName, displayExchange, dataSourceLabel, ext,
  } = q;
  const hasDayRange = high != null && low != null;

  const isMobile = useIsMobile();
  const [metricsCollapsed, setMetricsCollapsed] = useState(false);

  const [tickTime, setTickTime] = useState<Date | null>(null);
  useEffect(() => {
    if (tickAt) setTickTime(new Date(tickAt));
  }, [tickAt]);

  const formatTickTime = (date: Date | null): string | null => {
    if (!date) return null;
    return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  const watchlistBtn = onOpenWatchlist ? (
    <button className="stock-metrics-watchlist-pill" onClick={onOpenWatchlist}>
      <List size={13} />
      Watchlist
    </button>
  ) : null;

  return (
    <div className={`stock-header${isMobile && metricsCollapsed ? ' stock-header--compact' : ''}`}>
      <div className="stock-header-top">
        <div>
          <div className="stock-title">
            {onSwitchSymbol ? (
              <SymbolSwitcher symbol={symbol} onPick={onSwitchSymbol} />
            ) : (
              <span className="stock-symbol">{symbol}</span>
            )}
            <span className="stock-name">{displayName}</span>
            {displayExchange && <span className="stock-exchange">{displayExchange}</span>}
            <span className="stock-data-source stock-data-source--inline">
              <span className={`data-source-dot data-source-dot--${status}`} />
              <span className="data-source-label">
                {status === 'live' ? t('marketView.quote.live') : getVenueStatusLabel(symbol, t(`marketView.quote.${status}`))}
              </span>
              {status === 'live' && tickTime && <span className="data-source-time">{formatTickTime(tickTime)}</span>}
              <span className="data-source-tooltip">
                <span>{t('marketView.quote.source', { label: dataSourceLabel })}</span>
                <span>WebSocket: {wsStatus === 'connected' ? (wsHasData ? `Connected (${wsDataLevel === 'second' ? 'second' : 'minute'}-level)` : 'Connected (no data)') : wsStatus === 'disabled' ? 'Not available' : wsStatus === 'reconnecting' ? 'Reconnecting' : 'Disconnected'}</span>
              </span>
            </span>
          </div>
          <div className="stock-header-actions">
            <HeaderPill onClick={onToggleOverview}>
              <Info size={13} />
              {t('marketView.companyOverview')}
            </HeaderPill>
            {headerActions}
          </div>
        </div>
        <div className="stock-price-section">
          {/* In an extended session the headline is the official close, stable
              across refreshes and intervals; the session move rides beneath it
              against its own anchor, in session color. */}
          <div className={`stock-price ${headline.tone}`}>{fmt(headline.price)}</div>
          {(headline.change != null && headline.pct != null) ? (
            <div className={`stock-change ${headline.tone}`}>
              {signedFixed2(headline.change)} {signedFixed2(headline.pct)}%
            </div>
          ) : !ext && (
            <div className={`stock-change ${headline.tone}`}>{DASH}</div>
          )}
          {ext && (
            <ExtendedHoursPair
              ext={ext}
              iconSize={13}
              className="stock-extended-hours"
              style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.8125rem' }}
            />
          )}
        </div>
      </div>

      {isMobile && (
        <div className="stock-metrics-toggle-row">
          <button
            className="stock-metrics-toggle"
            onClick={() => setMetricsCollapsed(c => !c)}
            aria-expanded={!metricsCollapsed}
          >
            <span>{metricsCollapsed ? 'Show metrics' : 'Hide metrics'}</span>
            <ChevronDown size={14} className={`stock-metrics-toggle-icon${metricsCollapsed ? '' : ' stock-metrics-toggle-icon--open'}`} />
          </button>
          {watchlistBtn}
        </div>
      )}
      <div
        className={`stock-metrics-wrapper${isMobile && metricsCollapsed ? ' stock-metrics-wrapper--collapsed' : ''}`}
      >
        <div className="stock-metrics">
          <div className="metric-item">
            <span className="metric-label">Prev Close</span>
            <span className="metric-value">{fmt(previousClose)}</span>
          </div>
          <div className="metric-item">
            <span className="metric-label">Open
              <span className="metrics-discrepancy-hint" title="Values are aggregated from intraday data and may differ slightly from daily figures shown on the chart.">!</span>
            </span>
            <span className="metric-value">{fmt(open)}</span>
          </div>
          <div className="metric-item">
            <span className="metric-label">Low</span>
            <span className="metric-value">{fmt(low)}</span>
          </div>
          <div className="metric-item">
            <span className="metric-label">High</span>
            <span className="metric-value">{fmt(high)}</span>
          </div>
          <div className="metric-item">
            <span className="metric-label">52 wk high</span>
            <span className="metric-value">{fmt(fiftyTwoWeekHigh)}</span>
          </div>
          <div className="metric-item">
            <span className="metric-label">52 wk low</span>
            <span className="metric-value">{fmt(fiftyTwoWeekLow)}</span>
          </div>
          <div className="metric-item">
            <span className="metric-label">Avg Vol (3M)</span>
            <span className="metric-value">
              {averageVolume != null ? compactNumberFixed2(averageVolume) : DASH}
            </span>
          </div>
          <div className="metric-item">
            <span className="metric-label">{volumeIsAverage ? 'Avg Vol (3M)' : 'Volume'}</span>
            <span className="metric-value">
              {shownVolume != null ? compactNumberFixed2(shownVolume) : DASH}
            </span>
          </div>
          <div className="metric-item">
            <span className="metric-label">Day Range</span>
            <span className="metric-value">
              {hasDayRange ? `${fixed2(low)} – ${fixed2(high)}` : DASH}
            </span>
          </div>
          <div className="metric-item">
            <span className="metric-label">{ext ? 'Change % incl. ext' : 'Change %'}</span>
            <span className={`metric-value ${(changePercent ?? 0) < 0 ? 'negative' : 'positive'}`}>
              {changePercent != null ? `${signedFixed2(changePercent)}%` : DASH}
            </span>
          </div>
          {!isMobile && onOpenWatchlist && (
            <div className="metric-item metric-item--watchlist">
              {watchlistBtn}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default React.memo(StockHeader);
