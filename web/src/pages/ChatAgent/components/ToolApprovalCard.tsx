import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Check, ChevronRight, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Loader } from '@/components/ui/loader';
import type { ToolApprovalState } from '@/types/chat';
import { ArgsTable } from './mcp/ArgsTable';
import { DirectToolTileMark } from './mcp/DirectToolMark';
import { useDirectToolVendorLabel } from './mcp/useDirectToolVendor';
import { humanizeKey } from '../utils/structuredResult';

interface ToolApprovalCardProps {
  data: ToolApprovalState | null;
  onApprove?: () => void;
  onReject?: (message?: string) => void;
}

/**
 * Inline card for a direct MCP tool call that stopped for approval (a live
 * order, say). Pending shows the vendor, the tool and every argument exactly
 * as it will be sent, with Approve and Reject plus an optional reason; a
 * settled card collapses to a status row that still opens to the arguments.
 * With no handlers the pending card is a record of a stop nobody can answer
 * now, so it names that rather than asking, and shows no spinner.
 */
function ToolApprovalCard({ data, onApprove, onReject }: ToolApprovalCardProps): React.ReactElement | null {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(true);
  const [reason, setReason] = useState('');
  const vendorLabel = useDirectToolVendorLabel(data?.server || '');

  if (!data) return null;

  const { status, server, tool, args } = data;
  const toolLabel = humanizeKey(tool);
  const isApproved = status === 'approved';
  const isRejected = status === 'rejected';

  const argsBlock = (
    <div className="rounded-lg px-4 py-3" style={{ border: '1px solid var(--color-border-muted)' }}>
      <ArgsTable args={args || {}} emptyLabel={t('toolArtifact.directTool.noArguments')} />
    </div>
  );

  if (isApproved || isRejected) {
    return (
      <div>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="flex items-center gap-2 py-1 cursor-pointer w-full text-left"
        >
          <motion.div animate={{ rotate: collapsed ? 0 : 90 }} transition={{ duration: 0.2 }}>
            <ChevronRight className="h-3.5 w-3.5 flex-shrink-0" style={{ color: 'var(--color-icon-muted)' }} />
          </motion.div>
          {isApproved ? (
            <Check className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-accent-light)' }} />
          ) : (
            <X className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
          )}
          <span
            className="text-sm truncate"
            style={{ color: isApproved ? 'var(--color-text-tertiary)' : 'var(--color-text-quaternary)' }}
          >
            {isApproved
              ? t('toolArtifact.directTool.approvedAction', { vendor: vendorLabel, tool: toolLabel })
              : t('toolArtifact.directTool.rejectedAction', { vendor: vendorLabel, tool: toolLabel })}
          </span>
          {isRejected && data.reason && (
            <span className="text-xs truncate" style={{ color: 'var(--color-icon-muted)' }}>
              {data.reason}
            </span>
          )}
        </button>
        <AnimatePresence initial={false}>
          {!collapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
              className="overflow-hidden"
            >
              <div className="pt-2 pb-1 pl-6" style={{ opacity: isRejected ? 0.6 : 0.8 }}>
                {argsBlock}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }

  const canAct = !!onApprove || !!onReject;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
      data-testid="tool-approval-card"
    >
      <div className="flex items-center gap-3 pb-3">
        <DirectToolTileMark server={server} />
        <div className="min-w-0 flex-1">
          <div className="text-[0.9375rem] font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
            {canAct
              ? t('toolArtifact.directTool.approvalTitle')
              : t('toolArtifact.directTool.unansweredTitle')}
          </div>
          <div className="text-xs truncate" style={{ color: 'var(--color-text-tertiary)' }}>
            {vendorLabel} · {toolLabel}
          </div>
        </div>
        {canAct && (
          <Loader size={14} className="ml-auto flex-shrink-0 text-[color:var(--color-icon-muted)]" />
        )}
      </div>

      {argsBlock}

      {canAct && (
        <div className="pt-3 flex flex-wrap items-center gap-2">
          <motion.button
            type="button"
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); onApprove?.(); }}
            disabled={!onApprove}
            className="flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium transition-colors hover:brightness-110 disabled:opacity-50"
            style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Check className="h-3.5 w-3.5 stroke-[2.5]" />
            {t('toolArtifact.directTool.approve')}
          </motion.button>
          <motion.button
            type="button"
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); onReject?.(reason.trim() || undefined); }}
            disabled={!onReject}
            className="flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium transition-colors disabled:opacity-50"
            style={{ backgroundColor: 'var(--color-border-muted)', color: 'var(--color-text-tertiary)' }}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <X className="h-3.5 w-3.5" />
            {t('toolArtifact.directTool.reject')}
          </motion.button>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={200}
            placeholder={t('toolArtifact.directTool.reasonPlaceholder')}
            aria-label={t('toolArtifact.directTool.reasonPlaceholder')}
            className="flex-1 min-w-[10rem] text-sm px-3 py-2 rounded-md bg-transparent outline-none focus:ring-1"
            style={{ border: '1px solid var(--color-border-muted)', color: 'var(--color-text-primary)' }}
          />
        </div>
      )}
    </motion.div>
  );
}

export default ToolApprovalCard;
