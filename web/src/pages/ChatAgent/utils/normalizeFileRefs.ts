/**
 * Centralized file reference normalization.
 *
 * AI agents are inconsistent in how they format file references in markdown.
 * This function canonicalizes all known variants into clean relative paths
 * BEFORE markdown parsing, so downstream components only need to handle:
 *
 *   - Same-workspace relative:   results/report.md
 *   - Cross-workspace qualified: __wsref__/{uuid}/results/report.md
 *
 * Runs once per message render as a content-level pre-processing step.
 *
 * Every label below is bounded to 512, for the reason the secretary's twin
 * already records (`src/tools/secretary/utils.py`): these patterns are
 * unanchored, so on a run of `[` with no `]` an unbounded label rescans the
 * line from every one of them. That is quadratic, and it runs on the main
 * thread for every bubble, so a bracket-heavy unfenced line froze the tab for
 * seconds. 512 is past any real link label.
 */

import { mapOutsideCode } from './markdownSegments';

/**
 * Step 1: Unwrap backtick-wrapped markdown links.
 *
 * Agents sometimes wrap file links in backticks, making them render as code
 * spans instead of clickable links:
 *   `[report.md](results/report.md)` → [report.md](results/report.md)
 *   `![chart](charts/fig.png)`       → ![chart](charts/fig.png)
 *
 * Only unwraps when the ENTIRE code span is a single markdown link.
 * Does not touch multi-backtick code blocks or inline code with other content.
 */
const BACKTICK_LINK_RE = /`(!?\[[^\]]{0,512}\]\([^)]+\))`/g;

/**
 * Step 2: Unwrap `file://` from a sandbox href, keeping the path rooted.
 *
 * rehype-sanitize drops non-whitelisted protocols like file://, so the scheme
 * has to go before parsing. The root does not: `/home/workspace/` is how a
 * reference says it starts at the workspace rather than beside the file
 * quoting it, and `agentPaths.takeApart` reads that and canonicalizes it for
 * every caller. Flattening it here instead left a link indistinguishable from
 * a relative one, so a link to `/home/workspace/results/report.md` read from
 * `docs/index.md` opened `docs/results/report.md` when both existed.
 *   [report.md](file:///home/workspace/results/report.md)
 *     → [report.md](/home/workspace/results/report.md)
 * It also reaches inside an angle-bracketed destination and keeps the brackets.
 */
const FILE_PROTO_RE = /(!?\[[^\]]{0,512}\]\(<?)file:\/\/(?=\/home\/(?:workspace|daytona)\/)/g;

/**
 * Step 3: Clean stale prefixes inside __wsref__ paths.
 *
 * Legacy stored messages may contain artifacts from before the backend fix:
 *   __wsref__/uuid/file:///home/workspace/x → __wsref__/uuid/x
 *   __wsref__/uuid//home/workspace/x        → __wsref__/uuid/x
 */
const WSREF_INNER_RE = /(__wsref__\/[0-9a-f-]+\/)(?:file:\/\/)?\/home\/(?:workspace|daytona)\//g;

/**
 * Step 4: Wrap link destinations that contain spaces in angle brackets.
 *
 * CommonMark ends a bare destination at the first space, so
 * `[deck](results/Q3 deck.pptx)` renders as plain text with nothing to click.
 * `<...>` is the destination form that allows spaces. Only file-like
 * destinations (ending in an extension, no scheme, no title) are rewritten.
 *   [deck](results/Q3 deck.pptx) → [deck](<results/Q3 deck.pptx>)
 */
const LINK_DEST_RE = /(!?\[[^\]\n]{0,512}\]\()((?:[^()<>"'\n]|\([^()\n]*\))+)\)/g;

function wrapSpacedDestination(match: string, open: string, dest: string): string {
  const trimmed = dest.trim();
  if (!/\s/.test(trimmed)) return match;
  if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return match;
  // An extension holds a letter, so a parenthetical like `(up 2.5)` stays prose.
  // A fragment may hold spaces, because `findHeadingIndex` takes a heading as
  // written and not only its slug, so `report.md#Valuation Assumptions` is a
  // reference the panel can already open and the link has to survive to reach it.
  if (!/\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8}(?:#[^<>\n]*|:\d+(?:-\d+|:\d+)?)?$/.test(trimmed)) return match;
  // Same reason as step 5: with no slash before the colon, `my model.py:` reads as a scheme.
  const anchored = /^[^/:#?]+:\d+(?:-\d+|:\d+)?$/.test(trimmed) ? `./${trimmed}` : trimmed;
  return `${open}<${anchored}>)`;
}

/**
 * Step 5: Anchor a bare `name.ext:42` destination with `./`.
 *
 * With no slash before the colon, the markdown URL filter reads `model.py:`
 * as a scheme and drops the href, so the link renders with nothing to click.
 *   [model](model.py:42)     → [model](./model.py:42)
 *   [model](<my model.py:42>) → [model](<./my model.py:42>)
 *
 * The title is optional and rides in its own group, because it is not part of
 * the destination and the `./` has to land before it. Requiring `)` right
 * after the line number instead left `[model](model.py:42 "source")` dead,
 * while `turnFiles.ts` collected the same reference into a card: the reader
 * got a card they could click beside prose they could not.
 */
const TITLE = String.raw`(?:[ \t]+(?:"[^"\n]{0,512}"|'[^'\n]{0,512}'|\([^()\n]{0,512}\)))?`;
const LINE_DEST = String.raw`\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8}:\d+(?:-\d+|:\d+)?`;
const BARE_LINE_DEST_RE = new RegExp(
  String.raw`(!?\[[^\]\n]{0,512}\]\()([^\s()<>/:#?]+${LINE_DEST})(${TITLE}\))`, 'g');
const BRACKETED_LINE_DEST_RE = new RegExp(
  String.raw`(!?\[[^\]\n]{0,512}\]\(<)([^\n()<>/:#?]+${LINE_DEST}>${TITLE}\))`, 'g');

/**
 * Normalize all file references in a markdown string.
 *
 * Run this ONCE before markdown parsing. After normalization, all file hrefs
 * are either clean relative paths or __wsref__/{uuid}/relative paths.
 */
export function normalizeFileRefs(content: string): string {
  if (!content || typeof content !== 'string') return content;

  content = content.replace(BACKTICK_LINK_RE, '$1');     // step 1
  content = content.replace(FILE_PROTO_RE, '$1');         // step 2
  content = content.replace(WSREF_INNER_RE, '$1');        // step 3
  // Steps 4 and 5 rewrite a destination's text, which inside code would change
  // what the reader copies.
  content = mapOutsideCode(content, (prose) => prose
    .replace(LINK_DEST_RE, wrapSpacedDestination)          // step 4
    .replace(BARE_LINE_DEST_RE, '$1./$2$3')                // step 5
    .replace(BRACKETED_LINE_DEST_RE, '$1./$2'));

  return content;
}
