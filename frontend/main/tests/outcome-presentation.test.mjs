import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// Render the actual components; no API calls, backend records or fixtures are mutated.
const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
try {
  const { OutcomeIcon } = await server.ssrLoadModule('/src/components/ui/OutcomeIcon.tsx');
  const { OutcomeBadge } = await server.ssrLoadModule('/src/components/ui/Badge.tsx');
  const { FindingCard } = await server.ssrLoadModule('/src/components/chat/FindingCard.tsx');
  const { FindingsTable } = await server.ssrLoadModule('/src/components/output/FindingsTable.tsx');
  const { OUTCOME_LABELS } = await server.ssrLoadModule('/src/data/outcomeLabels.ts');
  const shapes = new Set();
  const geometry = (svg) => svg.slice(svg.indexOf('>') + 1, svg.lastIndexOf('</svg>'));
  const render = (component, props) => renderToStaticMarkup(createElement(component, props));
  for (const outcome of Object.keys(OUTCOME_LABELS)) {
    const finding = Object.freeze({
      id: outcome, check_id: `TEST-${outcome}`, name: 'Synthetic outcome rendering test',
      outcome, severity: 'FLAG', reviewer_action: null,
      recorded_operands: Object.freeze([
        Object.freeze({ name: 'vendor', value: '9007199254740993 1/3 in', source: 'SHOP', status: 'APPROVED', hasEvidence: false }),
      ]),
    });
    const before = JSON.stringify(finding);
    const icon = render(OutcomeIcon, { outcome });
    shapes.add(geometry(icon));
    const variants = [
      render(OutcomeBadge, { outcome }),
      render(OutcomeBadge, { outcome, size: 'sm' }),
      render(FindingCard, { finding, isSelected: false, onViewEvidence() {}, onAction() {}, onCorrect() {}, onExcept() {} }),
      render(FindingsTable, { findings: [finding] }),
    ];
    for (const html of variants) {
      assert.ok(html.includes(OUTCOME_LABELS[outcome]), `${outcome}: visible shared wording is retained`);
      const icons = [...html.matchAll(/<svg\b[^>]*data-outcome-icon="([A-Z_]+)"[^>]*>[\s\S]*?<\/svg>/g)];
      assert.ok(icons.length > 0, `${outcome}: shared glyph is present`);
      for (const [svg, recordedOutcome] of icons) {
        assert.equal(recordedOutcome, outcome, 'rendering never changes the outcome');
        assert.match(svg, /aria-hidden="true"/, 'decorative glyph is not announced twice');
        assert.match(svg, /focusable="false"/, 'glyph does not create a keyboard stop');
        assert.equal(geometry(svg), geometry(icon), 'badge, card and table use identical glyph geometry');
      }
    }
    assert.ok(variants[3].includes('9007199254740993 1/3 in'), 'exact value remains verbatim');
    assert.equal(JSON.stringify(finding), before, 'presentation does not mutate the finding');
  }
  assert.equal(shapes.size, 5, 'each outcome remains distinguishable without relying on colour');
  console.log('outcome presentation: all five shared glyphs, labels, accessibility and exact values passed');
} finally {
  await server.close();
}
