/**
 * The files a turn produced, as a card deck under its last bubble.
 *
 * A long turn buries its deliverables: the paths are named somewhere in the
 * prose, or only inside a tool call nobody expands. The deck collects them
 * where the reader finishes reading, and collapses to a single card carrying
 * the count, so a turn that wrote six files still ends on one object.
 */
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Download, PanelRight } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { deckHeight, deckSlot, useDeckCollapse, type DeckGeometry } from '@/components/ui/cardDeck';
import { fileExtension, fileKind, fileKindIcon } from '../../utils/filePaths';
import type { OpenFileHandler } from '../../utils/fileLocation';
import { cardDownloadKey, downloadLabel, useDownloadState } from '../../utils/downloadNotice';
import type { TurnFile } from '../../utils/turnFiles';
import { useMessageActions } from './MessageActionsContext';
import './TurnFileCards.css';

/** Taller cards than the sources deck, since each carries a name and a meta line. */
const GEOMETRY: DeckGeometry = {
  cardHeight: 68,
  cardGap: 8,
  peekStep: 6,
  maxPeekLayers: 2,
  peekScaleStep: 0.02,
  minPeekScale: 0.9,
};

interface TurnFileCardsProps {
  files: TurnFile[];
  onOpenFile: OpenFileHandler;
  onDownloadFile?: (path: string, workspaceId?: string) => void;
  /** Asks the host to bring the unfolded deck into view. Walking up to a scroll
   *  container and moving it does not work here: the nearest ancestor reporting
   *  a scrollable `overflow-y` is the transcript's content column, which CSS
   *  resolves to `auto` only because its `overflow-x` is hidden and which never
   *  scrolls; and the host re-asserts its own scroll position on every growth
   *  frame of the fan. Only the host can do this. */
  onReveal?: () => void;
}

/** Its own component so each card can read whether its save is running. */
function CardDownloadItem({ file, onDownloadFile }: {
  file: { path: string; workspaceId?: string };
  onDownloadFile: (path: string, workspaceId?: string) => void;
}): React.ReactElement {
  const { t } = useTranslation();
  const { downloadKeyFor } = useMessageActions();
  const key = downloadKeyFor
    ? downloadKeyFor(file.path, file.workspaceId)
    : cardDownloadKey(file.workspaceId, file.path);
  const state = useDownloadState(key);
  return (
    <DropdownMenuItem onSelect={() => onDownloadFile(file.path, file.workspaceId)} disabled={state !== 'idle'}>
      <Download className="h-3.5 w-3.5" />
      {downloadLabel(state, t('chat.turnFiles.download'))}
    </DropdownMenuItem>
  );
}

export function TurnFileCards({ files, onOpenFile, onDownloadFile, onReveal }: TurnFileCardsProps): React.ReactElement | null {
  const { t } = useTranslation();
  const [fanned, setFanned] = useState(false);
  // A card's menu portals outside the deck, so an open menu suspends the
  // outside-click and Escape collapse: otherwise choosing Download would fold
  // the deck shut under the pointer, and Escape would close both at once.
  const [menuOpen, setMenuOpen] = useState(false);
  const n = files.length;
  const rootRef = useDeckCollapse({ open: fanned, onCollapse: () => setFanned(false), suspend: menuOpen });

  // Unfolding is two things: the deck opens, and the transcript makes room for
  // it. The second is the host's to do, so it is asked in the same breath.
  const fan = () => {
    setFanned(true);
    onReveal?.();
  };

  if (n === 0) return null;

  return (
    <div
      ref={rootRef}
      className="turn-files"
      data-testid="turn-files"
      data-fanned={fanned}
      style={{ height: deckHeight(n, fanned, GEOMETRY) }}
    >
      {files.map((file, i) => {
        const { style, interactive } = deckSlot(i, n, fanned, GEOMETRY);
        const summarizing = !fanned && i === 0 && n > 1;
        const name = file.path.split('/').pop() || file.path;
        const kind = fileKind(file.path);
        const Icon = kind ? fileKindIcon(kind) : null;
        const ext = fileExtension(file.path);
        const stat = file.stats ? `+${file.stats.added} -${file.stats.removed}` : '';
        const kindLine = [kind ? t(`chat.turnFiles.kind.${kind}`) : '', ext.toUpperCase(), stat]
          .filter(Boolean)
          .join(' · ');
        const open = () => onOpenFile(file.path, file.workspaceId, file.location);

        return (
          <div
            key={`${file.workspaceId ?? ''}/${file.path}`}
            aria-hidden={interactive ? undefined : true}
            className="turn-file-card"
            style={{ height: GEOMETRY.cardHeight, ...style }}
          >
            {/* A peek card behind the front renders as a blank sheet: its name
                would bleed out below the front card as a garbled tail. */}
            {interactive && (
              <>
                <span className="turn-file-thumb" aria-hidden="true">
                  <span className="turn-file-sheet" />
                  <span className="turn-file-page">
                    {Icon && <Icon className="turn-file-glyph" />}
                    <span className="turn-file-ext">{ext.toUpperCase()}</span>
                  </span>
                </span>
                {/* The whole stripe is the primary target: this button carries
                    the name for its accessible label and stretches over the
                    card, so the menu trigger is the only thing clicking it can
                    miss. */}
                <button
                  type="button"
                  className="turn-file-hit"
                  aria-label={summarizing
                    ? t('chat.turnFiles.expand', { count: n, name })
                    : t('chat.turnFiles.openTitle', { path: file.path })}
                  title={summarizing ? undefined : file.path}
                  onClick={() => (summarizing ? fan() : open())}
                >
                  <span className="turn-file-name">{name}</span>
                  <span className="turn-file-meta">{kindLine}</span>
                </button>
                {summarizing ? (
                  <span className="turn-file-count" aria-hidden="true">
                    <span className="turn-file-badge">{n}</span>
                    <ChevronDown className="h-4 w-4" />
                  </span>
                ) : (
                  <span className="turn-file-actions">
                    <span className="turn-file-open" aria-hidden="true">
                      <PanelRight className="h-3.5 w-3.5" />
                      {t('chat.turnFiles.open')}
                    </span>
                    <DropdownMenu modal={false} onOpenChange={setMenuOpen}>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          className="turn-file-more"
                          aria-label={t('chat.turnFiles.moreActions')}
                        >
                          <ChevronDown className="h-3.5 w-3.5" />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" sideOffset={4}>
                        <DropdownMenuItem onSelect={open}>
                          <PanelRight className="h-3.5 w-3.5" />
                          {t('chat.turnFiles.open')}
                        </DropdownMenuItem>
                        <DropdownMenuItem onSelect={() => onOpenFile(file.path, file.workspaceId, file.location, { pin: true })}>
                          <PanelRight className="h-3.5 w-3.5" />
                          {t('filePanel.openInNewTab')}
                        </DropdownMenuItem>
                        {onDownloadFile && (
                          <CardDownloadItem file={file} onDownloadFile={onDownloadFile} />
                        )}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </span>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
