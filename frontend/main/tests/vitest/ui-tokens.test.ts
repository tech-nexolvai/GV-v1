/**
 * The new colour tokens meet WCAG contrast in both themes (#1029).
 *
 * Read from src/styles/tailwind.css itself, so a token edit that breaks contrast fails here.
 * Text and glyphs need 4.5:1; fills (chart segments, bars) and control edges need 3:1.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const css = fs.readFileSync(path.resolve(__dirname, '../../src/styles/tailwind.css'), 'utf8');

function block(selectorStart: string): Record<string, string> {
  const start = css.indexOf(selectorStart);
  if (start < 0) throw new Error(`no ${selectorStart} block`);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  const tokens: Record<string, string> = {};
  for (const m of css.slice(open + 1, close).matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\b/g)) tokens[m[1]] = m[2];
  return tokens;
}

const light = block(':root,\n  [data-theme=\'light\']');
const dark = { ...light, ...block("[data-theme='dark'] {") };

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const OUTCOMES = ['pass', 'fail', 'review', 'missing'] as const;

describe.each([
  ['light', light],
  ['dark', dark],
])('%s theme', (_name, t) => {
  it.each(OUTCOMES)('%s text reads at 4.5:1 on its tint and on the page', (o) => {
    expect(contrast(t[`outcome-${o}-fg`], t[`outcome-${o}-bg`])).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t[`outcome-${o}-fg`], t.background)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t[`outcome-${o}-fg`], t.card)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(OUTCOMES)('%s fill stands out from the page at 3:1', (o) => {
    expect(contrast(t[`outcome-${o}`], t.background)).toBeGreaterThanOrEqual(3);
  });

  it('the filled "needs correction" badge reads at 4.5:1 (page colour on the fail colour)', () => {
    expect(contrast(t.background, t['outcome-fail-fg'])).toBeGreaterThanOrEqual(4.5);
  });

  it('body text, muted text and control edges are readable', () => {
    expect(contrast(t.foreground, t.background)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t['muted-foreground'], t.background)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t['muted-foreground'], t.muted)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t['primary-foreground'], t.primary)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(t.input, t.background)).toBeGreaterThanOrEqual(3);
  });
});
