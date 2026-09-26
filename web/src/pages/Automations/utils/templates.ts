import {
  TrendingUp,
  Sun,
  BarChart3,
  CalendarSearch,
  Plus,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import i18n from '@/i18n';
import { US_MARKET_TZ } from '@/lib/bars/exchanges';
import { INITIAL_FORM, type FormState } from './form';

export type TemplateId = 'price_alert' | 'morning_briefing' | 'weekly_review' | 'earnings_watch' | 'custom';

export interface AutomationTemplate {
  id: TemplateId;
  nameKey: string;
  descriptionKey: string;
  icon: LucideIcon;
  defaults: Partial<FormState>;
}

export const AUTOMATION_TEMPLATES: AutomationTemplate[] = [
  {
    id: 'custom',
    nameKey: 'automation.blankAutomation',
    descriptionKey: 'automation.blankAutomationDesc',
    icon: Plus,
    defaults: {},
  },
  {
    id: 'price_alert',
    nameKey: 'automation.templatePriceAlert',
    descriptionKey: 'automation.templatePriceAlertDesc',
    icon: TrendingUp,
    defaults: {
      trigger_type: 'price',
      agent_mode: 'flash',
      instruction:
        'Analyze recent news and market sentiment for {symbol} to explain the price movement. Include key catalysts, analyst reactions, and short-term outlook.',
    },
  },
  {
    id: 'morning_briefing',
    nameKey: 'automation.templateMorningBriefing',
    descriptionKey: 'automation.templateMorningBriefingDesc',
    icon: Sun,
    defaults: {
      trigger_type: 'cron',
      cron_expression: '0 7 * * 1-5',
      timezone: US_MARKET_TZ,
      agent_mode: 'flash',
      instruction:
        'Provide a pre-market briefing covering: overnight market moves, key economic data releases today, notable earnings reports, and sector trends to watch.',
    },
  },
  {
    id: 'weekly_review',
    nameKey: 'automation.templateWeeklyReview',
    descriptionKey: 'automation.templateWeeklyReviewDesc',
    icon: BarChart3,
    defaults: {
      trigger_type: 'cron',
      cron_expression: '0 22 * * 5',
      timezone: US_MARKET_TZ,
      agent_mode: 'ptc',
      instruction:
        "Analyze my portfolio's weekly performance. Create charts showing returns vs benchmarks, sector allocation, and risk metrics. Identify positions that need attention and suggest rebalancing actions.",
    },
  },
  {
    id: 'earnings_watch',
    nameKey: 'automation.templateEarningsWatch',
    descriptionKey: 'automation.templateEarningsWatchDesc',
    icon: CalendarSearch,
    defaults: {
      trigger_type: 'once',
      agent_mode: 'ptc',
      instruction:
        'Run a deep earnings analysis for {symbol}: review recent financial statements, analyst estimates, historical earnings surprises, options activity, and key metrics to watch. Create a comprehensive pre-earnings report.',
    },
  },
];

/** Recurring first, then the triggered and one-time kinds, blank last: a
 *  template is the easier start, and like cadences read side by side. */
export const STARTER_ORDER: TemplateId[] = ['morning_briefing', 'weekly_review', 'price_alert', 'earnings_watch', 'custom'];

export function templatesById(ids: TemplateId[]): AutomationTemplate[] {
  return ids.flatMap((id) => AUTOMATION_TEMPLATES.filter((tpl) => tpl.id === id));
}

/** `timezone` is the user's own zone. A template tied to the US session
 *  (the pre-market briefing) keeps New York, since its hour is a market's. */
export function applyTemplate(templateId: TemplateId, timezone = INITIAL_FORM.timezone): FormState {
  const template = AUTOMATION_TEMPLATES.find((t) => t.id === templateId);
  if (!template || template.id === 'custom') return { ...INITIAL_FORM, timezone };
  // The field starts as the name the menu showed, so it reads in the
  // reader's language. The blank start's label names a choice, not an
  // automation, so it starts empty.
  return { ...INITIAL_FORM, timezone, ...template.defaults, name: i18n.t(template.nameKey) };
}
