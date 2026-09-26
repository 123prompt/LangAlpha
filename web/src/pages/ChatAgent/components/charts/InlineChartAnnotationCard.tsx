/**
 * Inline preview for an agent ``chart_annotation`` artifact in the chat
 * transcript.
 *
 * On the standalone ChatAgent page (no live chart present) it is a full-bleed
 * "spotlight" card — the symbol's real (clean) price chart; ticker, latest
 * price, window change and an annotation legend float over soft scrims, all
 * from the same bars (annotations are listed, not drawn). A click opens the
 * symbol's chart tab in the host's panel: the workspace panel, where the
 * drawing is live, or a share's, which shows the prices alone. A mount with
 * no panel goes to the MarketView page instead.
 *
 * Inside the MarketView desktop panel the real chart already shows the drawing
 * live, so the card collapses to a one-line confirmation chip (see
 * ChartSurfaceContext).
 */

import React, { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { LineChart, Check, ArrowRight } from 'lucide-react';
import { useMessageActions } from '../messageList/MessageActionsContext';
import { useIsMobile } from '@/hooks/useIsMobile';

import type { StoredAnnotation } from '@/pages/MarketView/stores/chartAnnotationStore';
import {
  chartAnnotationStore,
  makeChartId,
  useDisplayCleared,
} from '@/pages/MarketView/stores/chartAnnotationStore';
import { useStockBars } from '@/pages/MarketView/hooks/useStockBars';
import { AUTO_FIT_BARS, INTERVAL_LABEL } from '@/lib/bars';
import { describeAnnotationVisual } from '@/pages/MarketView/utils/annotationGeometry';

import { useWorkspaceId } from '../../contexts/WorkspaceContext';
import { useChartSurface } from '../../contexts/ChartSurfaceContext';
import { AnnotationPreviewChart } from './AnnotationPreviewChart';
import { CARD_BG, CARD_BORDER } from './inlineCardsShared';
import { buildMarketViewUrl } from '@/pages/MarketView/utils/marketRoute';

const TEXT_COLOR = 'var(--color-text-tertiary)';
const ACCENT = 'var(--color-accent-primary)';
const FOCUS_RING = 'var(--color-focus-ring)';

const RESTING_SHADOW = '0 1px 2px rgba(0,0,0,0.05), 0 16px 36px -18px rgba(0,0,0,0.5)';
const RAISED_SHADOW = '0 2px 6px rgba(0,0,0,0.06), 0 26px 50px -20px rgba(0,0,0,0.6)';

// Centered overlay for the chart's loading / empty states.
const CENTERED: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};


// Translucent chrome that floats over the chart (theme-aware via color-mix).
const GLASS_BG = 'color-mix(in srgb, var(--color-bg-tool-card) 62%, transparent)';
const GLASS_BORDER = 'color-mix(in srgb, var(--color-text-primary) 16%, transparent)';
const SCRIM_TOP =
  'linear-gradient(to bottom, color-mix(in srgb, var(--color-bg-tool-card) 92%, transparent), transparent)';
const SCRIM_BOTTOM =
  'linear-gradient(to top, color-mix(in srgb, var(--color-bg-tool-card) 94%, transparent), transparent)';

// How many annotation chips to show in the floating legend before "+N".
const MAX_LEGEND = 3;

interface InlineChartAnnotationCardProps {
  artifact: Record<string, unknown> | null | undefined;
}

export function InlineChartAnnotationCard({
  artifact,
}: InlineChartAnnotationCardProps): React.ReactElement | null {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { onOpenChart } = useMessageActions();
  const params = useParams();
  const ctxWorkspaceId = useWorkspaceId();
  const { chartPresent, activeSymbol, activeTimeframe, onJumpToChart } = useChartSurface();
  const isMobile = useIsMobile();
  const reduceMotion = useReducedMotion();

  const symbol = ((artifact?.symbol as string) || '').toUpperCase();
  const timeframe = (artifact?.timeframe as string) || '1day';
  const annotations = useMemo(
    () => (artifact?.annotations as StoredAnnotation[] | undefined) ?? [],
    [artifact],
  );
  const workspaceId = (artifact?.workspace_id as string | undefined) || ctxWorkspaceId || undefined;
  const threadId = params.threadId as string | undefined;
  // A transcript mounted with no workspace is a share: the owner's hosts always
  // name one. The drawing lives in a workspace's store, so a chart opened from
  // here carries none, and the card must not promise it.
  const pricesOnly = ctxWorkspaceId === null;

  // Whether this instance is currently cleared from the chart (MarketView only).
  const displayCleared = useDisplayCleared(workspaceId, symbol, timeframe);

  // The card lifts for either device; only a keyboard also gets a ring, since
  // the rounded corners mean the outline is suppressed and drawn as a shadow.
  const [hover, setHover] = useState(false);
  const [keyboardFocus, setKeyboardFocus] = useState(false);
  const raised = hover || keyboardFocus;

  // Resting-card price preview: cached bars for this symbol/timeframe. Skipped
  // inside MarketView (chartPresent) where the card collapses to a chip.
  const { bars, isLoading: barsLoading } = useStockBars(symbol, timeframe, {
    enabled: !chartPresent,
  });

  // Re-apply a cleared drawing to the adjacent MarketView chart.
  const handleRestore = useCallback(() => {
    if (!workspaceId || !symbol) return;
    chartAnnotationStore.restoreDisplay(workspaceId, makeChartId(symbol, timeframe));
  }, [workspaceId, symbol, timeframe]);

  const handleOpenInMarketView = useCallback(() => {
    if (!symbol) return;
    navigate(buildMarketViewUrl({
      symbol,
      timeframe,
      workspaceId,
      threadId: threadId !== '__default__' ? threadId : null,
      returnTo: location.pathname + location.search,
    }));
  }, [symbol, timeframe, workspaceId, threadId, location, navigate]);

  // The chart tab beside the chat is where the drawing is live, so the card
  // goes straight there. A mount without a panel to land in still has the
  // MarketView page. Either way the card asks for the drawing, so one the user
  // cleared from that chart comes back, as the MarketView chip does it.
  const handleOpen = useCallback(() => {
    if (!symbol) return;
    if (workspaceId) chartAnnotationStore.restoreDisplay(workspaceId, makeChartId(symbol, timeframe));
    if (onOpenChart) onOpenChart({ symbol, timeframe, workspaceId });
    else handleOpenInMarketView();
  }, [symbol, timeframe, workspaceId, onOpenChart, handleOpenInMarketView]);

  if (!artifact || !symbol) return null;

  const count = annotations.length;

  // Inside MarketView: the real chart shows the drawing — collapse to a chip.
  // The chip is clickable. Three states:
  //  - different instance than what's on screen → jump the chart to it;
  //  - this instance but cleared from the chart → re-apply it;
  //  - this instance and showing → a passive confirmation.
  if (chartPresent) {
    const isActiveInstance =
      (activeSymbol ?? '').toUpperCase() === symbol &&
      (!activeTimeframe || activeTimeframe === timeframe);
    const canJump = !!onJumpToChart && !isActiveInstance;
    // Accent border invites a click whenever one would change the chart.
    const accented = canJump || displayCleared;

    const handleChipClick = (): void => {
      if (canJump) {
        onJumpToChart?.(symbol, timeframe);
        // Un-clear so the drawing shows once the chart switches to it.
        if (workspaceId) {
          chartAnnotationStore.restoreDisplay(workspaceId, makeChartId(symbol, timeframe));
        }
      } else {
        handleRestore();
      }
    };

    const title = canJump
      ? t('chat.chartAnnotationCard.chipJumpTitle', {
          symbol,
          timeframe: INTERVAL_LABEL[timeframe] ?? timeframe,
        })
      : displayCleared
        ? t('chat.chartAnnotationCard.chipShowTitle')
        : t('chat.chartAnnotationCard.chipShownTitle');

    return (
      <button
        type="button"
        onClick={handleChipClick}
        title={title}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 8,
          background: CARD_BG,
          border: `1px solid ${accented ? ACCENT : CARD_BORDER}`,
          borderRadius: 999,
          padding: '6px 12px',
          fontSize: '0.75rem',
          color: TEXT_COLOR,
          cursor: 'pointer',
          transition: 'border-color 0.15s',
        }}
        onMouseEnter={(e) => (e.currentTarget.style.borderColor = ACCENT)}
        onMouseLeave={(e) =>
          (e.currentTarget.style.borderColor = accented ? ACCENT : CARD_BORDER)
        }
      >
        {accented
          ? <LineChart size={14} style={{ color: ACCENT, flexShrink: 0 }} />
          : <Check size={14} style={{ color: 'var(--color-profit)', flexShrink: 0 }} />}
        <span>
          <span style={{ color: 'var(--color-text-primary)', fontWeight: 600 }}>{symbol}</span>
          <span style={{ color: TEXT_COLOR }}>{` · ${INTERVAL_LABEL[timeframe] ?? timeframe}`}</span>
          {' · '}
          {canJump
            ? t('chat.chartAnnotationCard.chipViewCount', { count })
            : displayCleared
              ? t('chat.chartAnnotationCard.chipShowCount', { count })
              : t('chat.chartAnnotationCard.chipShownCount', { count })}
        </span>
        {canJump && <ArrowRight size={13} style={{ color: ACCENT, flexShrink: 0 }} />}
      </button>
    );
  }

  // Render the same recent window the live chart auto-fits to (not the whole
  // fetched history), so the header %-change and curve match what opens.
  const fitBars = AUTO_FIT_BARS[timeframe] ?? 180;
  const viewBars = bars.length > fitBars ? bars.slice(-fitBars) : bars;

  // Price + window change from those same bars, so the header never disagrees
  // with the curve underneath it.
  const lastClose = viewBars.length ? viewBars[viewBars.length - 1].close : null;
  const firstClose = viewBars.length ? viewBars[0].close : null;
  const pct =
    lastClose != null && firstClose ? ((lastClose - firstClose) / firstClose) * 100 : null;
  const up = pct == null || pct >= 0;
  const trendColor = up ? 'var(--color-profit)' : 'var(--color-loss)';
  const hasChart = !barsLoading && viewBars.length >= 2;
  const plotHeight = isMobile ? 200 : 248;

  // The floating legend names the real annotations (they're listed here, not
  // drawn over the preview curve — the full set shows when the chart opens).
  const visuals = annotations.map(describeAnnotationVisual);
  const shownVisuals = visuals.slice(0, MAX_LEGEND);
  const extraCount = visuals.length - shownVisuals.length;

  // A clean full-bleed price chart: ticker and price float over soft scrims,
  // an annotation legend sits bottom-left, and the CTA opens the chart tab
  // (where the annotations are drawn).
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={t(
        pricesOnly ? 'chat.chartAnnotationCard.cardAriaPricesOnly' : 'chat.chartAnnotationCard.cardAria',
        { symbol, timeframe, count },
      )}
      onClick={handleOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          handleOpen();
        }
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      // :focus-visible is the only thing that answers whether a keyboard
      // brought focus here; the ring stays off a click. The colour is the
      // shared focus token rather than the accent, which reads as selected.
      onFocus={(e) => setKeyboardFocus(e.currentTarget.matches(':focus-visible'))}
      onBlur={() => setKeyboardFocus(false)}
      style={{
        position: 'relative',
        background: CARD_BG,
        border: `1px solid ${keyboardFocus ? FOCUS_RING : raised ? ACCENT : CARD_BORDER}`,
        borderRadius: 20,
        overflow: 'hidden',
        cursor: 'pointer',
        // The ring is a shadow, because the card's 20px corners want one that
        // follows them. Forced colors drops shadows, so the keyboard state
        // also carries a transparent outline: invisible here, painted in the
        // system's focus color there, and the only indicator left in it.
        outline: keyboardFocus ? '2px solid transparent' : 'none',
        userSelect: 'none',
        transform: raised ? 'translateY(-2px)' : 'none',
        boxShadow: keyboardFocus
          ? `0 0 0 2px ${FOCUS_RING}, ${RAISED_SHADOW}`
          : raised ? RAISED_SHADOW : RESTING_SHADOW,
        transition: 'border-color 0.16s, box-shadow 0.16s, transform 0.16s',
      }}
    >
      {/* Full-bleed plot — the real chart, or a loading / empty fallback. */}
      <div style={{ position: 'relative', height: plotHeight }}>
        {barsLoading ? (
          <div style={CENTERED}>
            <span style={{ fontSize: '0.75rem', color: TEXT_COLOR }}>
              {t('chat.chartAnnotationCard.loadingChart')}
            </span>
          </div>
        ) : hasChart ? (
          // Clean price line only — the legend below conveys the annotations.
          <AnnotationPreviewChart
            bars={viewBars}
            trendColor={trendColor}
            showLastPrice
          />
        ) : (
          <div style={CENTERED}>
            <span
              style={{
                display: 'inline-flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 6,
                color: TEXT_COLOR,
              }}
            >
              <LineChart size={26} style={{ opacity: 0.5 }} />
              <span style={{ fontSize: '0.75rem' }}>
                {t('chat.chartAnnotationCard.previewUnavailable')}
              </span>
            </span>
          </div>
        )}

        {/* Scrims keep the floating chrome legible over the chart. */}
        {hasChart && (
          <>
            <div
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                right: 0,
                height: 78,
                background: SCRIM_TOP,
                pointerEvents: 'none',
              }}
            />
            <div
              style={{
                position: 'absolute',
                bottom: 0,
                left: 0,
                right: 0,
                height: 92,
                background: SCRIM_BOTTOM,
                pointerEvents: 'none',
              }}
            />
          </>
        )}

        {/* Top-left — ticker, latest price, window change. */}
        <div
          style={{
            position: 'absolute',
            top: 15,
            left: 17,
            display: 'flex',
            alignItems: 'baseline',
            gap: 9,
            minWidth: 0,
          }}
        >
          <span
            style={{
              fontSize: '1.3125rem',
              fontWeight: 700,
              color: 'var(--color-text-primary)',
              letterSpacing: '-0.01em',
            }}
          >
            {symbol}
          </span>
          {lastClose != null && (
            <span style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--color-text-primary)' }}>
              ${lastClose.toLocaleString('en-US', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}
            </span>
          )}
          {pct != null && (
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 3,
                fontSize: '0.8125rem',
                fontWeight: 600,
                color: trendColor,
              }}
            >
              <span
                style={{
                  width: 0,
                  height: 0,
                  borderLeft: '3.5px solid transparent',
                  borderRight: '3.5px solid transparent',
                  ...(up
                    ? { borderBottom: `5px solid ${trendColor}` }
                    : { borderTop: `5px solid ${trendColor}` }),
                }}
              />
              {pct >= 0 ? '+' : ''}
              {pct.toFixed(2)}%
            </span>
          )}
        </div>

        {/* Top-right — timeframe pill. */}
        <span
          style={{
            position: 'absolute',
            top: 16,
            right: 16,
            fontSize: '0.6875rem',
            fontWeight: 700,
            letterSpacing: '0.03em',
            color: TEXT_COLOR,
            background: GLASS_BG,
            border: `1px solid ${GLASS_BORDER}`,
            backdropFilter: 'blur(8px)',
            padding: '4px 9px',
            borderRadius: 7,
          }}
        >
          {INTERVAL_LABEL[timeframe] ?? timeframe}
        </span>

        {/* Bottom-left — annotation legend (the real annotations). */}
        {hasChart && shownVisuals.length > 0 && (
          <div
            style={{
              position: 'absolute',
              bottom: 15,
              left: 17,
              display: 'flex',
              flexWrap: 'wrap',
              alignItems: 'center',
              gap: 14,
              maxWidth: '62%',
            }}
          >
            {/* `initial={false}` keeps the first paint (and history replay)
                instant; only annotations that arrive while the pinned card is
                already mounted animate in, so the legend grows smoothly as the
                agent draws. */}
            <AnimatePresence initial={false}>
              {shownVisuals.map((v, i) => (
                <motion.span
                  key={annotations[i]?.annotation_id || `legend-${i}`}
                  initial={reduceMotion ? false : { opacity: 0, y: 3, scale: 0.96 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: -3, scale: 0.96 }}
                  transition={{ duration: reduceMotion ? 0 : 0.2, ease: 'easeOut' }}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    fontSize: '0.7188rem',
                    fontWeight: 600,
                    color: 'var(--color-text-secondary)',
                  }}
                >
                  <span
                    style={{ width: 8, height: 8, borderRadius: 2.5, backgroundColor: v.color, flexShrink: 0 }}
                  />
                  <span
                    style={{
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      maxWidth: 130,
                    }}
                  >
                    {v.label}
                  </span>
                </motion.span>
              ))}
            </AnimatePresence>
            {extraCount > 0 && (
              <span style={{ fontSize: '0.7188rem', fontWeight: 600, color: TEXT_COLOR }}>+{extraCount}</span>
            )}
          </div>
        )}

        {/* Bottom-right — CTA (glass → accent whenever the card is raised).
            The accent fill is the sanctioned exception recorded in DESIGN.md. */}
        <span
          style={{
            position: 'absolute',
            bottom: 14,
            right: 14,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 7,
            fontSize: '0.8125rem',
            fontWeight: 600,
            padding: '9px 15px',
            borderRadius: 11,
            backdropFilter: 'blur(8px)',
            background: raised ? ACCENT : GLASS_BG,
            border: `1px solid ${raised ? 'transparent' : GLASS_BORDER}`,
            color: raised ? 'var(--color-text-on-accent)' : 'var(--color-text-primary)',
            transition: 'background 0.16s, color 0.16s, border-color 0.16s',
          }}
        >
          {t(pricesOnly ? 'chat.chartAnnotationCard.openChart' : 'chat.chartAnnotationCard.openAnnotatedChart')}
          <ArrowRight
            size={14}
            style={{ transform: raised ? 'translateX(3px)' : 'none', transition: 'transform 0.16s' }}
          />
        </span>
      </div>
    </div>
  );
}

export default InlineChartAnnotationCard;
