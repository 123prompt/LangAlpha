import { createContext, useContext } from 'react';
import type { UserPreferences } from '../types/api';

/** Both displays fold finished work behind a summary row. `lean` is the
 *  default; `verbose` also expands reasoning while it streams. */
export type TurnDisplay = 'verbose' | 'lean';
/** How reply text lands while streaming: per token, or held to paragraph
 *  boundaries so the reader never watches a sentence being typed. */
export type StreamingMode = 'token' | 'paragraph';

const TURN_DISPLAY_KEY = 'turn_display';
const STREAMING_MODE_KEY = 'response_streaming_mode';

function readOther(prefs: UserPreferences | null | undefined, key: string): unknown {
  const other = prefs?.other_preference;
  return typeof other === 'object' && other !== null && !Array.isArray(other)
    ? (other as Record<string, unknown>)[key]
    : undefined;
}

/** `other_preference.turn_display`; anything but 'verbose' reads as 'lean'. */
export function readTurnDisplay(prefs: UserPreferences | null | undefined): TurnDisplay {
  return readOther(prefs, TURN_DISPLAY_KEY) === 'verbose' ? 'verbose' : 'lean';
}

/** `other_preference.response_streaming_mode`; anything but 'paragraph' reads as 'token'. */
export function readStreamingMode(prefs: UserPreferences | null | undefined): StreamingMode {
  return readOther(prefs, STREAMING_MODE_KEY) === 'paragraph' ? 'paragraph' : 'token';
}

/** One-key patches: the server merges `other_preference` shallowly, so sending
 *  the key alone never writes a stale sibling back over another tab's value. */
export function turnDisplayPatch(next: TurnDisplay): Partial<UserPreferences> {
  return { other_preference: { [TURN_DISPLAY_KEY]: next } };
}
export function streamingModePatch(next: StreamingMode): Partial<UserPreferences> {
  return { other_preference: { [STREAMING_MODE_KEY]: next } };
}

export interface TranscriptDisplay {
  turnDisplay: TurnDisplay;
  streamingMode: StreamingMode;
}

export const DEFAULT_TRANSCRIPT_DISPLAY: TranscriptDisplay = { turnDisplay: 'lean', streamingMode: 'token' };

/** Hosts that never mount the provider (shared chat, market view) get the
 *  lean, token-streamed transcript. */
export const TranscriptDisplayContext = createContext<TranscriptDisplay>(DEFAULT_TRANSCRIPT_DISPLAY);

export function useTranscriptDisplay(): TranscriptDisplay {
  return useContext(TranscriptDisplayContext);
}
