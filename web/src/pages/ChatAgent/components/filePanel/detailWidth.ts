import type { ToolCallProcessRecord } from '../ToolCallDetailView';
import { isTaskTool } from '../toolDisplayConfig';

/** What a plan's text opens at. */
export const PLAN_TAB_WIDTH = 550;
/** What the file panel opens at before a drag has said otherwise. */
export const DEFAULT_PANEL_WIDTH = 850;

/** How wide a tool result wants the panel: a filing or a file read needs the room, a data card does not. */
export function detailPanelWidth(toolCallProcess: ToolCallProcessRecord | null): number {
  if (!toolCallProcess) return PLAN_TAB_WIDTH;
  const toolName = toolCallProcess.toolName || '';
  const artifactType = toolCallProcess.toolCallResult?.artifact?.type;
  if (artifactType === 'sec_filing') return DEFAULT_PANEL_WIDTH;
  if (toolName === 'Read') return DEFAULT_PANEL_WIDTH;
  if (isTaskTool(toolName)) return 750;
  if (artifactType === 'stock_prices' || artifactType === 'market_indices' || artifactType === 'sector_performance') return 650;
  if (toolName === 'WebSearch' || toolName === 'web_search') return 650;
  if (artifactType === 'company_overview' || artifactType === 'automations') return 480;
  return 650;
}
