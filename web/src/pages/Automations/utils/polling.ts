export const IDLE_POLL_MS = 30_000;
/** A run takes seconds to minutes, and its row has to move from the loader
 *  to the result without a reload. */
export const LIVE_POLL_MS = 5_000;

/** How often a list refetches. Only something starting or running polls
 *  fast (`isRunLive`, `isAutomationRunning`): a wait can last half an hour,
 *  and the user feed already announces both its start and its end. */
export function pollMs(live: boolean): number {
  return live ? LIVE_POLL_MS : IDLE_POLL_MS;
}
