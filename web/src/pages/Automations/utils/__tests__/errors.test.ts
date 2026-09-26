import { describe, expect, it } from 'vitest';
import { mutationErrorMessage } from '../errors';

const failure = (detail: unknown, message = 'Request failed with status code 422') => ({
  message,
  response: { status: 422, data: { detail } },
});

describe('mutationErrorMessage', () => {
  it('reads a validation list as the fields and sentences it names', () => {
    const err = failure([
      { loc: ['body', 'trigger_config', 'conditions', 0, 'value'], msg: 'Input should be greater than 0', type: 'greater_than' },
      { loc: ['body', 'trigger_config', 'symbol'], msg: "Value error, Use bare symbol (e.g. 'SPX', not '^SPX')", type: 'value_error' },
    ]);
    expect(mutationErrorMessage(err, 'fallback')).toBe(
      "value: Input should be greater than 0; symbol: Use bare symbol (e.g. 'SPX', not '^SPX')",
    );
  });

  it('keeps a plain detail as it is', () => {
    expect(mutationErrorMessage(failure('Automation not found'), 'fallback')).toBe('Automation not found');
  });

  it('falls back when nothing readable came back', () => {
    expect(mutationErrorMessage({}, 'Something went wrong')).toBe('Something went wrong');
    expect(mutationErrorMessage(failure([{ loc: ['body'] }], ''), 'Something went wrong')).toBe('Something went wrong');
  });
});
