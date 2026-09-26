import { beforeAll, describe, expect, it } from 'vitest';
import i18n from '@/i18n';
import { excelSerialToDate, formatCellValue, formatDate, formatNumber, isDateFormat, splitSections } from '../numFmt';

// Every expectation below is en-US output; pin the locale so a machine whose
// navigator says otherwise still runs the same assertions.
beforeAll(async () => {
  await i18n.changeLanguage('en-US');
});

describe('splitSections', () => {
  it('splits on a real separator only', () => {
    expect(splitSections('#,##0;(#,##0);"-"')).toEqual(['#,##0', '(#,##0)', '"-"']);
    expect(splitSections('0.0"a;b"')).toEqual(['0.0"a;b"']);
    expect(splitSections('[>=100]0;0.0')).toEqual(['[>=100]0', '0.0']);
  });
});

describe('formatNumber', () => {
  it('reads a percent as a percent', () => {
    expect(formatNumber(0.1234, '0.0%')).toBe('12.3%');
    expect(formatNumber(0.1234, '0%')).toBe('12%');
    expect(formatNumber(0.5, '0.00%')).toBe('50.00%');
  });

  it('groups thousands and holds the decimals the code asks for', () => {
    expect(formatNumber(1624, '#,##0')).toBe('1,624');
    expect(formatNumber(1624.456, '#,##0.00')).toBe('1,624.46');
    expect(formatNumber(1624, '#,##0.00')).toBe('1,624.00');
    expect(formatNumber(1624, '0')).toBe('1624');
    expect(formatNumber(0.5, '0.00')).toBe('0.50');
  });

  it('carries a currency symbol, written either way', () => {
    expect(formatNumber(1234.5, '$#,##0.00')).toBe('$1,234.50');
    expect(formatNumber(1234.5, '"$"#,##0.00')).toBe('$1,234.50');
    expect(formatNumber(1234.5, '[$€-407]#,##0.00')).toBe('€1,234.50');
  });

  it('formats the magnitude under a negative section, which is what the parens are for', () => {
    expect(formatNumber(-1240, '#,##0;(#,##0)')).toBe('(1,240)');
    expect(formatNumber(1240, '#,##0;(#,##0)')).toBe('1,240');
    expect(formatNumber(-1240, '#,##0')).toBe('-1,240');
    expect(formatNumber(0, '#,##0;(#,##0);"--"')).toBe('--');
  });

  it('scales on a trailing comma, a thousand each', () => {
    expect(formatNumber(1234567, '#,##0.0,,"M"')).toBe('1.2M');
    expect(formatNumber(1234567, '#,##0,')).toBe('1,235');
  });

  it('reads a scientific code', () => {
    expect(formatNumber(12345, '0.00E+00')).toBe('1.23E4');
  });

  it('falls back to the raw value rather than guess', () => {
    expect(formatNumber(1234567, 'General')).toBe('1234567');
    expect(formatNumber(0.1234, '')).toBe('0.1234');
    // A fraction code means numerator/denominator, not a decimal.
    expect(formatNumber(0.5, '# ?/?')).toBe('0.5');
    expect(formatNumber(42, '"units"')).toBe('42');
  });

  it('spells General to eleven significant digits, with an exponent past the plain range', () => {
    expect(formatNumber(123456.789, 'General')).toBe('123456.789');
    expect(formatNumber(1e-12, 'General')).toBe('1e-12');
    expect(formatNumber(1.234e-8, 'General')).toBe('1.234e-8');
    expect(formatNumber(0.0001234, 'General')).toBe('0.0001234');
    expect(formatNumber(123456789012, 'General')).toBe('123456789012');
    expect(formatNumber(1.79e308, 'General')).toBe('1.79e+308');
    expect(formatNumber(0.1 + 0.2, 'General')).toBe('0.3');
    expect(formatNumber(0, 'General')).toBe('0');
  });

  it('falls back on a conditional section rather than pick one by sign', () => {
    expect(formatNumber(-5, '[>=100]0.0;[<100]0.00')).toBe('-5');
    expect(formatNumber(500, '[>=1000000]0.0,,"M";[>=1000]0.0,"K";0')).toBe('500');
    // A colour may lead the condition; by sign 5000 would read as `0.0M`.
    expect(formatNumber(5000, '[Blue][>=1000000]0.0,,"M";[Red][>=1000]0.0,"K";0')).toBe('5000');
    expect(formatNumber(-3, '[Red][<0]0.0;0.0')).toBe('-3');
    // A colour on its own is not a condition, and the sections still hold.
    expect(formatNumber(-1240, '#,##0;[Red](#,##0)')).toBe('(1,240)');
  });
});

describe('isDateFormat', () => {
  it('says yes only when every letter run is a date token', () => {
    expect(isDateFormat('yyyy-mm-dd')).toBe(true);
    expect(isDateFormat('mmm-yy')).toBe(true);
    expect(isDateFormat('[$-409]d mmmm yyyy')).toBe(true);
    expect(isDateFormat('h:mm:ss AM/PM')).toBe(true);
    expect(isDateFormat('"Day "d')).toBe(true);
  });

  it('says no to a number code that merely holds letters', () => {
    expect(isDateFormat('General')).toBe(false);
    expect(isDateFormat('#,##0.00')).toBe(false);
    expect(isDateFormat('0.00E+00')).toBe(false);
    expect(isDateFormat('#,##0 "days"')).toBe(false);
    expect(isDateFormat('0.0%')).toBe(false);
    expect(isDateFormat(undefined)).toBe(false);
  });
});

describe('formatCellValue', () => {
  it('turns a serial under a date code into that calendar day, laid out as the code says', () => {
    expect(excelSerialToDate(45000).toISOString().slice(0, 10)).toBe('2023-03-15');
    expect(formatCellValue(45000, 'yyyy-mm-dd')).toBe('2023-03-15');
    expect(formatCellValue(45000, 'mmm-yy')).toBe('Mar-23');
    expect(formatCellValue(45000, 'm/d/yyyy')).toBe('3/15/2023');
  });

  it('counts the 1900 system past its phantom leap day, and the 1904 system from its own day zero', () => {
    const day = (serial: number, date1904 = false) => excelSerialToDate(serial, { date1904 }).toISOString().slice(0, 10);
    expect(day(1)).toBe('1900-01-01');
    expect(day(59)).toBe('1900-02-28');
    // Serial 60 is 1900-02-29, a day that never was; it prints as the next real one.
    expect(day(60)).toBe('1900-03-01');
    expect(day(61)).toBe('1900-03-01');
    expect(day(45000)).toBe('2023-03-15');
    expect(day(0, true)).toBe('1904-01-01');
    expect(day(45000, true)).toBe('2027-03-16');
    expect(formatCellValue(45000, 'yyyy-mm-dd', { date1904: true })).toBe('2027-03-16');
    expect(formatCellValue(1, 'yyyy-mm-dd')).toBe('1900-01-01');
  });

  it('formats a Date the parser already resolved', () => {
    expect(formatCellValue(new Date(Date.UTC(2023, 2, 15)), 'dd/mm/yyyy')).toBe('15/03/2023');
  });

  it('honors month names, bare day numbers and two-digit years', () => {
    const day = new Date(Date.UTC(2024, 0, 5));
    expect(formatDate(day, 'mmm yyyy')).toBe('Jan 2024');
    expect(formatDate(day, 'd-mmm')).toBe('5-Jan');
    expect(formatDate(day, 'mmmm d, yyyy')).toBe('January 5, 2024');
    expect(formatDate(day, 'dd/mm/yy')).toBe('05/01/24');
    expect(formatDate(day, 'dddd')).toBe('Friday');
    expect(formatDate(day, '[$-409]d mmmm yyyy')).toBe('5 January 2024');
    expect(formatDate(day, '"Day "d')).toBe('Day 5');
  });

  it('reads m as minutes only beside an hour or a second', () => {
    const at = new Date(Date.UTC(2024, 0, 5, 14, 7, 9));
    expect(formatDate(at, 'h:mm')).toBe('14:07');
    expect(formatDate(at, 'hh:mm:ss AM/PM')).toBe('02:07:09 PM');
    expect(formatDate(at, 'h:mm AM/PM')).toBe('2:07 PM');
    expect(formatDate(at, 'mm:ss')).toBe('07:09');
    expect(formatDate(at, 'yyyy-mm-dd h:mm')).toBe('2024-01-05 14:07');
  });

  it('shows the raw serial under an elapsed-time or fractional-second code rather than invent a time', () => {
    expect(formatCellValue(1.5, '[h]:mm:ss')).toBe('1.5');
    expect(formatCellValue(45000.5, 'h:mm:ss.0')).toBe('45000.5');
    // A resolved Date has a day to show, so the day stands in for the time.
    expect(formatDate(new Date(Date.UTC(2024, 0, 5, 14, 7, 9)), '[mm]:ss')).toBe('2024-01-05');
  });

  it('passes text, booleans and blanks through', () => {
    expect(formatCellValue('Revenue ($M)')).toBe('Revenue ($M)');
    expect(formatCellValue(true)).toBe('TRUE');
    expect(formatCellValue(null)).toBe('');
    expect(formatCellValue(undefined)).toBe('');
  });

  it('never reads a number as a date without a date code', () => {
    expect(formatCellValue(45000, '#,##0')).toBe('45,000');
  });
});
