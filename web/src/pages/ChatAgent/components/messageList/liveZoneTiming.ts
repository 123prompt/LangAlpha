/**
 * Live-zone timing: the exposure window plus the row enter spring and exit
 * tween. A leaf module with no imports, and it has to stay one: the e2e
 * live-zone spec reads it from Node, where the rest of the render-block
 * module graph (i18n JSON) cannot load.
 */

/** Minimum time a just-completed item stays in the live zone before folding. */
export const MIN_LIVE_EXPOSURE_MS = 1800;

/** Enter spring for live-zone rows, damped to just under critical (2*sqrt(200) is 28.3)
 *  so a row never overshoots its height and swings back. */
export const SPRING_SNAPPY = { type: 'spring' as const, stiffness: 200, damping: 28 };
/** Quick tween for live rows clearing out, exits should not draw the eye. */
export const EXIT_TWEEN = { duration: 0.18, ease: 'easeIn' as const };
