/**
 * The card state for a direct MCP tool call stopped for approval. Both
 * projections build it from the same action request so a reload renders the
 * card the live stream did.
 */

import type { ToolApprovalState } from '@/types/chat';
import type { ActionRequest } from '@/types/sse';
import { isDirectToolName, parseDirectToolName } from '../../utils/directTools';

/** Whether an interrupt's first action request is a direct MCP tool call. */
export function isToolApprovalRequest(request: ActionRequest | undefined): boolean {
  return !!request && isDirectToolName(request.name);
}

export function buildToolApprovalState(
  request: ActionRequest,
  interruptId: string | undefined,
  actionIndex: number,
  actionCount: number,
): ToolApprovalState {
  const name = request.name || '';
  const parsed = parseDirectToolName(name) || { server: '', tool: name };
  return {
    toolName: name,
    server: parsed.server,
    tool: parsed.tool,
    args: request.args && typeof request.args === 'object' ? request.args : {},
    interruptId,
    actionIndex,
    actionCount,
    status: 'pending',
    reason: null,
  };
}

/**
 * One card per stopped call, keyed so the two projections agree.
 *
 * A turn that places two orders arrives as ONE interrupt carrying both action
 * requests, and the resume must answer every one of them. Rendering only the
 * first left the second call invisible and the resume short a decision, which
 * the server rejects outright.
 */
export function toolApprovalCards(
  actionRequests: ActionRequest[],
  interruptId: string | undefined,
  fallbackId: string,
): Array<{ proposalId: string; state: ToolApprovalState }> {
  const base = interruptId || fallbackId;
  return actionRequests.map((request, index) => ({
    proposalId: actionRequests.length > 1 ? `${base}#${index}` : base,
    state: buildToolApprovalState(request, interruptId, index, actionRequests.length),
  }));
}

/**
 * The verdict to write on the cards of an interrupt that stopped several calls,
 * or null for an interrupt that stopped one, whose own verdict is knowable.
 *
 * A resume persists one answer per interrupt, never one per call: approving
 * AAPL and rejecting MSFT records exactly what rejecting both records (see
 * `hitl_answers` in the server's `process_hitl_response`). No reject in a batch
 * is attributable to a particular call, so these cards read approved instead of
 * taking the interrupt-level verdict onto every one of them. The other floor
 * puts a rejected badge on an order that is executing, and a user who reads
 * that places it a second time. It is a floor, not evidence: only a
 * per-decision record on the resume turn can replace it with the truth.
 *
 * That record is `hitl_decisions`, read by `toolApprovalDecisionFields`. This
 * floor is what a thread persisted before the server kept it still gets.
 */
export function batchToolApprovalFields(
  cardCount: number,
): { status: 'approved'; reason: null } | null {
  return cardCount > 1 ? { status: 'approved', reason: null } : null;
}

/** One entry of the resume's `hitl_decisions`, in the shape the server records
 *  a `HITLDecision` in: the verdict, plus the user's own message if they gave
 *  one. */
export interface HitlDecision {
  type?: string;
  message?: string | null;
}

/**
 * The per-interrupt decision lists a resume turn recorded, or undefined for a
 * thread persisted before the server kept them.
 */
export function readHitlDecisions(
  metadata: Record<string, unknown> | undefined,
): Record<string, HitlDecision[]> | undefined {
  const raw = metadata?.hitl_decisions;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  return raw as Record<string, HitlDecision[]>;
}

/**
 * The action request a card answers, recovered from the id `toolApprovalCards`
 * minted for it: the batch suffix, or 0 for the single call that has none.
 */
export function toolApprovalActionIndex(
  proposalId: string,
  interruptId: string | undefined,
): number {
  if (interruptId && proposalId === interruptId) return 0;
  const index = Number(proposalId.slice(proposalId.lastIndexOf('#') + 1));
  return Number.isInteger(index) && index >= 0 ? index : 0;
}

/**
 * One card's own verdict, read out of the decision the resume recorded for its
 * action request, or null when the resume recorded none.
 *
 * This is the evidence `batchToolApprovalFields` says can replace its floor:
 * slot i answers action request i, so a mixed batch replays each call the way
 * the user answered it, reject reason included.
 */
export function toolApprovalDecisionFields(
  decisions: HitlDecision[] | undefined,
  actionIndex: number,
): { status: 'approved' | 'rejected'; reason: string | null } | null {
  const decision = decisions?.[actionIndex];
  if (!decision || (decision.type !== 'approve' && decision.type !== 'reject')) {
    return null;
  }
  const rejected = decision.type === 'reject';
  const message = typeof decision.message === 'string' ? decision.message.trim() : '';
  return { status: rejected ? 'rejected' : 'approved', reason: rejected && message ? message : null };
}
