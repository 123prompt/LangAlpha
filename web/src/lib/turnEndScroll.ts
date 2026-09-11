import type { UserPreferences } from '../types/api';

/** Where the transcript lands when a turn completes: stay at the end (the
 *  default), or bring the start of the final reply under the viewport top. */
export type TurnEndScroll = 'bottom' | 'reply_start';

const TURN_END_SCROLL_KEY = 'turn_end_scroll';

/** `other_preference.turn_end_scroll`; anything but 'reply_start' reads as 'bottom'. */
export function readTurnEndScroll(prefs: UserPreferences | null | undefined): TurnEndScroll {
  const other = prefs?.other_preference;
  const value =
    typeof other === 'object' && other !== null && !Array.isArray(other)
      ? (other as Record<string, unknown>)[TURN_END_SCROLL_KEY]
      : undefined;
  return value === 'reply_start' ? 'reply_start' : 'bottom';
}

/** The one-key patch for the preference. The server merges `other_preference`
 *  shallowly, so sending the key alone is what keeps a stale cached sibling
 *  from being written back over another tab's newer value. */
export function turnEndScrollPatch(next: TurnEndScroll): Partial<UserPreferences> {
  return { other_preference: { [TURN_END_SCROLL_KEY]: next } };
}
