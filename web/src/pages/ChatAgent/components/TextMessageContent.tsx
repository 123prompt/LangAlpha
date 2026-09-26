import React, { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import Markdown from './Markdown';
import { useAnimatedText } from '@/components/ui/animated-text';
import { visibleParagraphPrefix } from '@/lib/paragraphGate';
import { useTranscriptDisplay } from '@/lib/transcriptDisplay';
import { parseErrorMessage, type ParsedError } from '../utils/parseErrorMessage';
import type { OpenFileHandler } from '../utils/fileLocation';
import { UPSTREAM_HINT_I18N_KEY, type StructuredError } from '@/utils/rateLimitError';

interface TextMessageContentProps {
  content: string;
  isStreaming: boolean;
  hasError: boolean;
  /** When the backend classified the failure (currently ``upstream`` only —
   *  internal errors render at the chat-input banner, not inline), use the
   *  structured fields instead of re-parsing the raw message text. */
  structuredError?: StructuredError;
  onOpenFile?: OpenFileHandler;
  /** Whether everything that has arrived is on screen. The typewriter trails
   *  the stream, so what follows this block can wait for it to catch up. */
  onRevealed?: (done: boolean) => void;
}

/**
 * TextMessageContent Component
 *
 * Renders text content from message_chunk events with content_type: text.
 * Supports markdown formatting including bold, italic, lists, code blocks, etc.
 */
function TextMessageContent({ content, isStreaming, hasError, structuredError, onOpenFile, onRevealed }: TextMessageContentProps): React.ReactElement | null {
  const { streamingMode } = useTranscriptDisplay();
  // Paragraph mode holds the sentence being written, so the typewriter has
  // nothing left to animate: a paragraph lands whole or not at all.
  const gated = streamingMode === 'paragraph' && isStreaming;
  const text = content || '';
  // Whatever this bubble has already painted stays painted. The gate is a
  // floor on what to show, never a reason to take words back: switching the
  // delivery preference mid-reply used to drop the text between the last
  // blank line and the caret, and a reply with no blank line yet vanished
  // whole. The mark is per stream, so a regenerate starts from nothing.
  const shownLenRef = useRef(0);
  if (!isStreaming) shownLenRef.current = 0;
  const target = gated ? visibleParagraphPrefix(text) : text;
  const visibleLen = Math.max(target.length, Math.min(shownLenRef.current, text.length));
  shownLenRef.current = visibleLen;
  const visible = text.slice(0, visibleLen);
  const displayText = useAnimatedText(visible, { enabled: isStreaming && !gated });
  // Measured against everything that has ARRIVED, not against what the gate
  // chose to release. What follows this block waits on this flag, so comparing
  // against the released prefix called a held-back paragraph "shown" and let an
  // activity block overtake the prose it belongs under.
  const revealed = !content || hasError || displayText === text;
  useEffect(() => { onRevealed?.(revealed); }, [onRevealed, revealed]);

  if (!content) {
    return null;
  }

  if (hasError) {
    if (structuredError?.kind === 'upstream') {
      return <StructuredErrorDisplay err={structuredError} fallbackText={content} />;
    }
    const parsed = parseErrorMessage(content);
    return <ErrorDisplay parsed={parsed} />;
  }

  // Nothing has crossed a paragraph boundary yet. Render nothing rather than an
  // empty Markdown root, which would take the block's vertical rhythm with it.
  if (gated && !visible) {
    return null;
  }

  return (
    <Markdown variant="chat" content={displayText} className="text-base" onOpenFile={onOpenFile} />
  );
}

interface ErrorDisplayProps {
  parsed: ParsedError;
}

/**
 * ErrorDisplay Component
 *
 * Renders a parsed error message in a clean, structured format.
 */
function ErrorDisplay({ parsed }: ErrorDisplayProps): React.ReactElement {
  return (
    <div
      className="flex gap-3 px-4 py-3 rounded-lg text-sm"
      style={{
        backgroundColor: 'var(--color-loss-soft)',
        border: '1px solid var(--color-border-loss)',
      }}
    >
      <AlertTriangle
        className="h-5 w-5 flex-shrink-0 mt-0.5"
        style={{ color: 'var(--color-loss)' }}
      />
      <div className="min-w-0 space-y-1">
        <div className="font-medium" style={{ color: 'var(--color-loss)' }}>
          {parsed.title}
        </div>
        {parsed.detail && (
          <div style={{ color: 'var(--color-text-tertiary)' }}>
            {parsed.detail}
          </div>
        )}
        {parsed.model && (
          <div
            className="inline-block px-2 py-0.5 rounded text-xs mt-1"
            style={{
              backgroundColor: 'var(--color-border-muted)',
              color: 'var(--color-text-tertiary)',
            }}
          >
            {parsed.model}
            {parsed.statusCode ? ` · ${parsed.statusCode}` : ''}
          </div>
        )}
      </div>
    </div>
  );
}

interface StructuredErrorDisplayProps {
  err: StructuredError;
  /** Raw error text to fall back to when ``err.message`` is empty. */
  fallbackText: string;
}

/**
 * StructuredErrorDisplay
 *
 * Inline error card for classified upstream-provider failures. Renders the
 * kind-aware headline (with status code when known), the raw error message,
 * and the hint list ("check your API key", etc.). Used instead of the
 * regex-based ``ErrorDisplay`` when the backend has already classified the
 * error source — ``parseErrorMessage`` can't tell "Authentication error" from
 * the provider apart from the kind-routed message our own service returns.
 */
function StructuredErrorDisplay({ err, fallbackText }: StructuredErrorDisplayProps): React.ReactElement {
  const { t } = useTranslation();
  // When the failure is attributable to a specific model, name it in the
  // headline; otherwise fall back to the generic "model provider" copy.
  const headline = err.model
    ? (err.statusCode
        ? t('chat.errorUpstreamHeadlineModelStatus', { model: err.model, status: err.statusCode })
        : t('chat.errorUpstreamHeadlineModel', { model: err.model }))
    : (err.statusCode
        ? t('chat.errorUpstreamHeadlineStatus', { status: err.statusCode })
        : t('chat.errorUpstreamHeadline'));
  const body = err.message || fallbackText;
  // Models other than the primary that were also tried before giving up. Only
  // shown when the middleware actually attempted more than one model.
  const alsoTried = (err.attemptedModels && err.attemptedModels.length > 1)
    ? err.attemptedModels.filter((m) => m.model !== err.model)
    : [];
  return (
    <div
      className="flex gap-3 px-4 py-3 rounded-lg text-sm"
      style={{
        backgroundColor: 'var(--color-loss-soft)',
        border: '1px solid var(--color-border-loss)',
      }}
    >
      <AlertTriangle
        className="h-5 w-5 flex-shrink-0 mt-0.5"
        style={{ color: 'var(--color-loss)' }}
      />
      <div className="min-w-0 space-y-1">
        <div className="font-medium" style={{ color: 'var(--color-loss)' }}>
          {headline}
        </div>
        <div className="break-words" style={{ color: 'var(--color-text-tertiary)' }}>
          {body}
        </div>
        {err.hints && err.hints.length > 0 && (
          <ul className="mt-1 list-disc pl-4 flex flex-col gap-0.5 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            {err.hints.map((h) => (
              <li key={h}>{t(UPSTREAM_HINT_I18N_KEY[h] ?? h)}</li>
            ))}
          </ul>
        )}
        {alsoTried.length > 0 && (
          <div className="mt-1 text-xs" style={{ color: 'var(--color-text-quaternary)' }}>
            {t('chat.errorAttemptedModels')}{' '}
            {alsoTried.map((m, i) => (
              <React.Fragment key={m.model}>
                {i > 0 && ', '}
                {/* Per-model error text lives in the tooltip, not inline. */}
                <span title={m.error || undefined}>{m.model}</span>
              </React.Fragment>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// memo'd: every streaming token re-renders MessageContentSegments, which maps
// over earlier text blocks with unchanged content. Default shallow compare on
// primitive props + stable onOpenFile (useCallback in ChatView/SharedChatView)
// skips Markdown's AST parse for those stable blocks. Non-primitive props added
// later must be referentially stable or memoization becomes a no-op.
export default React.memo(TextMessageContent);
// eslint-disable-next-line react-refresh/only-export-components
export { parseErrorMessage, ErrorDisplay, StructuredErrorDisplay };
