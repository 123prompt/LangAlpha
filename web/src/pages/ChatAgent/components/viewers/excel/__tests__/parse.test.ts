import { beforeAll, describe, expect, it } from 'vitest';
import ExcelJS from 'exceljs';
import i18n from '@/i18n';
import { parseWorkbook, type SheetData } from '../parse';

/**
 * A real ExcelJS round-trip, because every bug this locks was invisible to a
 * hand-built fixture: what `[object Object]` used to be is a formula the writer
 * left uncalculated, and openpyxl writes every formula that way.
 */
async function buildWorkbook(): Promise<ArrayBuffer> {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet('Model');
  wb.addWorksheet('Inputs');

  ws.getCell('A1').value = 'DCF model';
  ws.mergeCells('A1:C1');

  ws.getCell('A4').value = 'Units (M)';
  ws.getCell('B4').value = 12.4;
  ws.getCell('B4').numFmt = '#,##0.0';
  ws.getCell('C4').value = 0.1234;
  ws.getCell('C4').numFmt = '0.0%';

  // Calculated: a cached result rode along with the formula.
  ws.getCell('B5').value = { formula: 'B4*2', result: 24.8 };
  ws.getCell('B5').numFmt = '#,##0.0';

  // Uncalculated: a formula and nothing else.
  ws.getCell('B6').value = { formula: 'B5*Inputs!C3' } as ExcelJS.CellFormulaValue;

  ws.getCell('B7').value = { formula: 'B5/0', result: { error: '#DIV/0!' } };

  ws.getCell('B8').value = new Date(Date.UTC(2023, 2, 15));
  ws.getCell('B8').numFmt = 'yyyy-mm-dd';

  const out = await wb.xlsx.writeBuffer();
  return out as ArrayBuffer;
}

describe('parseWorkbook', () => {
  let model: SheetData;

  beforeAll(async () => {
    await i18n.changeLanguage('en-US');
    const sheets = await parseWorkbook(await buildWorkbook());
    model = sheets[0];
    expect(sheets.map((s) => s.name)).toEqual(['Model', 'Inputs']);
  });

  const at = (ref: string) => {
    const [, col, row] = /^([A-Z]+)(\d+)$/.exec(ref)!;
    const c = col.split('').reduce((n, ch) => n * 26 + (ch.charCodeAt(0) - 64), 0);
    return model.rows[Number(row) - 1][c - 1];
  };

  it('shows a formula with no cached value as its formula, flagged', () => {
    expect(at('B6').calculated).toBe(false);
    expect(at('B6').formula).toBe('B5*Inputs!C3');
    expect(at('B6').text).toBe('=B5*Inputs!C3');
    expect(at('B6').text).not.toContain('[object Object]');
    expect(model.uncalculated).toBe(1);
  });

  it('shows the cached result of a formula that has one', () => {
    expect(at('B5').calculated).toBe(true);
    expect(at('B5').formula).toBe('B4*2');
    expect(at('B5').text).toBe('24.8');
  });

  it('shows a cached error as the error', () => {
    expect(at('B7').error).toBe('#DIV/0!');
    expect(at('B7').text).toBe('#DIV/0!');
  });

  it('applies the number format the cell carries', () => {
    expect(at('B4').text).toBe('12.4');
    expect(at('C4').text).toBe('12.3%');
    expect(at('B8').text).toBe('2023-03-15');
  });

  it('spans a merge from its top-left and marks what it covers', () => {
    expect(at('A1').colSpan).toBe(3);
    expect(at('A1').text).toBe('DCF model');
    expect(at('B1').master).toEqual({ row: 1, col: 1 });
    expect(at('C1').master).toEqual({ row: 1, col: 1 });
  });

  it('keeps row 1 a data row, addressed from 1', () => {
    expect(model.rows[3][0].text).toBe('Units (M)');
    expect(model.totalRows).toBe(8);
  });
});
