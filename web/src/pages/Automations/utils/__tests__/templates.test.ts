import { describe, it, expect } from 'vitest';
import i18n from '@/i18n';
import { INITIAL_FORM } from '../form';
import { applyTemplate, AUTOMATION_TEMPLATES, type TemplateId } from '../templates';

describe('applyTemplate', () => {
  it('returns price trigger defaults for price_alert', () => {
    const form = applyTemplate('price_alert');
    expect(form.trigger_type).toBe('price');
    expect(form.agent_mode).toBe('flash');
    expect(form.name).toBe('Price alert with analysis');
    expect(form.instruction).toContain('sentiment');
  });

  it('returns cron defaults for morning_briefing', () => {
    const form = applyTemplate('morning_briefing');
    expect(form.trigger_type).toBe('cron');
    expect(form.cron_expression).toBe('0 7 * * 1-5');
    expect(form.timezone).toBe('America/New_York');
    expect(form.agent_mode).toBe('flash');
  });

  it('returns cron PTC defaults for weekly_review', () => {
    const form = applyTemplate('weekly_review');
    expect(form.trigger_type).toBe('cron');
    expect(form.cron_expression).toBe('0 22 * * 5');
    expect(form.agent_mode).toBe('ptc');
  });

  it('returns once PTC defaults for earnings_watch', () => {
    const form = applyTemplate('earnings_watch');
    expect(form.trigger_type).toBe('once');
    expect(form.agent_mode).toBe('ptc');
  });

  it('prefills the name the template menu shows, in the reader\'s language', async () => {
    expect(applyTemplate('morning_briefing').name).toBe('Morning market briefing');
    await i18n.changeLanguage('zh-CN');
    try {
      expect(applyTemplate('morning_briefing').name).toBe('早盘简报');
    } finally {
      await i18n.changeLanguage('en-US');
    }
  });

  it('returns INITIAL_FORM for custom', () => {
    const form = applyTemplate('custom');
    expect(form).toEqual(INITIAL_FORM);
  });

  it('returns INITIAL_FORM for unknown id', () => {
    const form = applyTemplate('nonexistent' as TemplateId);
    expect(form).toEqual(INITIAL_FORM);
  });

  it('defines exactly 5 templates', () => {
    expect(AUTOMATION_TEMPLATES).toHaveLength(5);
  });
});
