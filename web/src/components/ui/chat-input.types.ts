import type { WidgetContextSnapshot } from '@/pages/Dashboard/widgets/framework/contextSnapshot';

export interface FileAttachment {
  id: string;
  file: File;
  type: string;
  preview: string | null;
  uploadStatus: 'pending' | 'uploading' | 'complete';
  dataUrl: string | null;
}

export interface MentionedFile {
  path: string;
  snippet?: string;
  label?: string;
  lineStart?: number;
  lineEnd?: number;
  lineCount?: number;
  /**
   * Where inside the file the snippet came from, when lines are the wrong unit:
   * a spreadsheet range writes `Model!B4:D9`. Fragment syntax without the `#`,
   * so `@path#locator` round-trips back through `parseFragment` as a link the
   * agent can answer with.
   */
  locator?: string;
  source?: string;
}

export interface SlashCommand {
  type: string;
  name: string;
  skillName?: string;
  description?: string;
  aliases?: string[];
}

export interface ModelOptions {
  model: string | null;
  reasoningEffort: string | null;
  fastMode: boolean;
  /** Per-message market-watch toggle — stamps live prices for tracked tickers. */
  marketWatch?: boolean;
  /**
   * Widget context snapshots attached via the deck rail. Forwarded to the
   * backend as `additional_context` items of `type: "widget"`. Image-bearing
   * snapshots also produce a sibling `type: "image"` MultimodalContext item;
   * see `widgetSnapshotsToContexts` in `pages/ChatAgent/utils/fileUpload.ts`.
   */
  widgetSnapshots?: WidgetContextSnapshot[];
}

export interface ReadyAttachment {
  file: File;
  dataUrl: string | null;
  type: string;
  preview: string | null;
}

export type { Workspace } from '@/types/api';
