import React from 'react';
import { motion } from 'framer-motion';
import { X as XIcon } from 'lucide-react';
import { TextShimmer } from '@/components/ui/text-shimmer';

export function RunningTitle({ label }: { label: string }): React.ReactElement {
  return (
    <span className="titem-title inline-flex min-w-0" style={{ flexShrink: 1 }}>
      <TextShimmer
        as="span"
        className="truncate [--base-color:var(--Labels-Secondary)] [--base-gradient-color:var(--color-text-primary)]"
        duration={1.5}
      >
        {label}
      </TextShimmer>
    </span>
  );
}

export function RunningIcon({ running, children }: { running: boolean; children: React.ReactNode }): React.ReactElement {
  return (
    <motion.span
      animate={running ? { opacity: [0.85, 1, 0.85] } : { opacity: 1 }}
      transition={running ? { duration: 1.5, repeat: Infinity, ease: 'easeInOut' } : { duration: 0.2 }}
      style={{ display: 'inline-flex' }}
    >
      {children}
    </motion.span>
  );
}

/** The settled half of a row (pill, chevron) fading in where the shimmer was.
 *  A row that was never live mounts settled and skips the fade. */
export function SettledLine({ wasRunning, children }: { wasRunning: boolean; children: React.ReactNode }): React.ReactElement {
  return (
    <motion.span
      className="contents"
      initial={wasRunning ? { opacity: 0 } : false}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2, ease: 'easeOut' }}
    >
      {children}
    </motion.span>
  );
}

export function FailedIconBadge({ label }: { label: string }): React.ReactElement {
  return (
    <span className="nrow-badge" aria-label={label}>
      <XIcon className="h-3 w-3" />
    </span>
  );
}
