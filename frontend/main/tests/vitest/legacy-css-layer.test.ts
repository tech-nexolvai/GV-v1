/**
 * Every hand-written stylesheet stays inside the `legacy` cascade layer (#1029).
 *
 * The layer is what keeps the old pages looking exactly as they did once Tailwind arrived: a
 * stylesheet left outside it would beat every Tailwind utility and every legacy rule regardless of
 * specificity — a quiet restyle nobody asked for. New styles belong in Tailwind classes; a new
 * plain .css file wraps itself the same way (first rule `@layer legacy {`, last line
 * `} /* @layer legacy *\/`).
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const SRC = path.resolve(__dirname, '../../src');

function cssFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === 'styles' && dir === SRC ? [] : cssFiles(full);
    return entry.name.endsWith('.css') ? [full] : [];
  });
}

function withoutComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

describe('legacy stylesheets', () => {
  const files = cssFiles(SRC);

  it('finds the legacy stylesheets', () => {
    // 8 since #1129 retired the old chat's stylesheets (ChatThread, ThinkingStream, StreamingText,
    // ChatInput, EvidencePanel, FindingCard and PdfViewer). Earlier: #1124 MeasurementPanel.css,
    // DrawingParts.css, SlotReaderRows.css and AssignmentProgress.css; #1125 PageFrame.css,
    // WelcomePage.css and NewReviewForm.css; #1072 CompanySettingsPage.css, RulebookPage.css and
    // UsagePage.css; #1064 PackagesPage.css and SignedDownloads.css; #1045 DrawingResultPanel.css;
    // #1039 ResultsPanel.css. The number only guards against the walk finding nothing.
    expect(files.length).toBeGreaterThanOrEqual(8);
  });

  it.each(files.map((f) => [path.relative(SRC, f), f]))('%s is wrapped in @layer legacy', (_name, file) => {
    const css = fs.readFileSync(file, 'utf8');
    const code = withoutComments(css).trim();
    // Only @import lines may come before the layer block (they must stay top-level).
    const firstRule = code.split('\n').find((line) => line.trim() && !line.trim().startsWith('@import'));
    expect(firstRule?.trim()).toBe('@layer legacy {');
    expect(css.trimEnd().endsWith('} /* @layer legacy */')).toBe(true);

    const opens = (code.match(/\{/g) ?? []).length;
    const closes = (code.match(/\}/g) ?? []).length;
    expect(closes).toBe(opens);
  });
});
