import React, { useEffect, useRef } from 'react';
import { SPRING_FOLD } from './liveZoneTiming';
import { useTranslation } from 'react-i18next';
import { ChevronDown } from 'lucide-react';
import { motion } from 'framer-motion';
import { formatWorkedFor } from '@/lib/elapsed';
import { announceAnchoredToggle } from '../../utils/anchoredToggle';

export interface TurnFoldProps {
  /** `live` while the turn runs; afterwards the header is the fold's switch.
   *  `process` is a turn with no answer, open to its cards with the
   *  accordions shut: it already looks open, so the chevron points down. */
  state: 'live' | 'collapsed' | 'process' | 'expanded';
  /** Epoch ms of the user message that opened the turn. */
  startedAt: number;
  /** Epoch ms the turn settled. Absent on a turn whose end was never recorded. */
  completedAt?: number;
  onToggle: () => void;
}

/**
 * One box for both states, so a turn settling changes the words inside the row
 * and nothing else: same hairline, same ink, same height. A row that grew a
 * border or a chevron's worth of height at the moment a turn finished would
 * shift the whole answer under the reader's eye.
 */
const ROW_STYLE: React.CSSProperties = {
  paddingTop: '5px',
  paddingBottom: '5px',
  marginBottom: '0.5rem',
  borderBottom: '1px solid var(--color-border-muted)',
  fontSize: '0.8125rem',
  color: 'var(--Labels-Tertiary)',
};


const CONTROL_STYLE: React.CSSProperties = { color: 'inherit', font: 'inherit' };

/**
 * The header that stands in for a turn's process: a running clock while the
 * turn works, then the switch that unfolds what it did.
 */
export function TurnFold({ state, startedAt, completedAt, onToggle }: TurnFoldProps): React.ReactElement {
  const { t } = useTranslation();

  return (
    <div className="flex items-center" style={ROW_STYLE} data-turn-fold={state}>
      {state === 'live' ? (
        <WorkingLabel startedAt={startedAt} />
      ) : (
        <button
          type="button"
          aria-expanded={state !== 'collapsed'}
          onClick={(e) => { announceAnchoredToggle(e.currentTarget); onToggle(); }}
          className="inline-flex items-center gap-1 text-left bg-transparent border-0 p-0 cursor-pointer transition-colors hover:text-foreground"
          style={CONTROL_STYLE}
        >
          <span className="tabular-nums">
            {completedAt === undefined
              ? t('chat.worked')
              : t('chat.workedFor', { duration: formatWorkedFor(completedAt - startedAt, t) })}
          </span>
          <motion.span
            animate={{ rotate: state === 'collapsed' ? 0 : 90 }}
            transition={SPRING_FOLD}
            className="inline-flex flex-shrink-0"
            style={{ opacity: 0.6 }}
          >
            <ChevronDown className="h-3.5 w-3.5 -rotate-90" />
          </motion.span>
        </button>
      )}
    </div>
  );
}

/**
 * The elapsed label writes its own text node once a second. A turn runs for
 * minutes beside a stream that already commits on every chunk; putting the
 * clock through React state would add a commit per second to that, for one
 * word that no other part of the tree reads.
 */
function WorkingLabel({ startedAt }: { startedAt: number }): React.ReactElement {
  const { t } = useTranslation();
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const write = () => {
      const el = ref.current;
      if (el) el.textContent = t('chat.workingFor', { duration: formatWorkedFor(Date.now() - startedAt, t) });
    };
    write();
    const id = setInterval(write, 1000);
    return () => clearInterval(id);
  }, [startedAt, t]);

  return (
    <span ref={ref} className="tabular-nums">
      {t('chat.workingFor', { duration: formatWorkedFor(Date.now() - startedAt, t) })}
    </span>
  );
}

export default TurnFold;
