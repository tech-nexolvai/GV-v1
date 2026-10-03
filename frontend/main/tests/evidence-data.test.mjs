import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';

// Use Vite's real module loader (including import.meta.env and CSS) without listening on a port.
// Every request is intercepted: no live package, stored evidence or backend data is changed.
const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
const originalFetch = globalThis.fetch;
try {
  const { toFinding, withChain, loadFindings } = await server.ssrLoadModule('/src/api/findings.ts');
  const { EvidencePanel } = await server.ssrLoadModule('/src/components/chat/EvidencePanel.tsx');
  const { FindingCard } = await server.ssrLoadModule('/src/components/chat/FindingCard.tsx');
  assert.match(readFileSync(new URL('../src/components/chat/ChatThread.tsx', import.meta.url), 'utf8'),
    /<FindingCard\s[^>]*\bdefaultExpanded\b/, 'chat table explicitly opens embedded detail cards');
  const row = (id) => ({ id, rule_id: `RULE-${id}`, outcome: 'FAIL', severity: 'FLAG', reviewer_action: null, rule_version: '1' });
  const location = (id, role = 'SHOP') => ({
    canonical_observation_id: id, document_version_id: 'doc', document_role: role,
    page_index: 12, page_id: 'page', semantic_type: 'cabinet_width', authority: 'reviewer-confirmed',
    coordinate_space: 'pdf_points', crop_uri: 'recorded-crop',
    polygon: [['9007199254740993.125', '10'], ['12', '10'], ['12', '14']],
  });
  const chain = {
    finding_id: 'one', outcome: 'FAIL', severity: 'FLAG', engine_version: '1', parameter_versions: {}, rule_snapshot: {},
    operands: [
      { name: 'first_width', numerator: '101', denominator: '4', unit: 'in', evidence_status: 'ELIGIBLE', evidence: location('first') },
      { name: 'second_width', numerator: '9007199254740993', denominator: '1', unit: 'in', evidence_status: 'ELIGIBLE', evidence: location('second') },
      { name: 'third_width', numerator: '2', denominator: '3', unit: 'in', evidence_status: 'ELIGIBLE', evidence: location('third', 'REFERENCE') },
    ],
    trace: { kind: 'calculation', operation: 'equals', operands: [{ name: 'first_width', value: '101/4 in', source: 'SHOP' }], comparison: '101/4 != 51/2' },
  };
  const finding = withChain(toFinding(row('one')), chain);
  let actions = 0;
  const cardProps = { isSelected: false, onViewEvidence() {}, onAction() { actions += 1; }, onCorrect() {}, onExcept() {} };
  for (const outcome of ['PASS', 'FAIL', 'REVIEW_REQUIRED', 'NOT_FOUND', 'NO_APPLICABLE_RULE']) {
    const props = { ...cardProps, finding: { ...finding, outcome } };
    const detail = renderToStaticMarkup(createElement(FindingCard, { ...props, defaultExpanded: true }));
    assert.match(detail, /class="finding-card__header" aria-expanded="true"/, `${outcome}: table details do not require a second disclosure`);
    assert.match(detail, /class="collapsible" data-open="true"><div class="finding-card__body"/, 'review body is open and not inert');
    if (outcome === 'NOT_FOUND') assert.doesNotMatch(detail, />Confirm<\/button>/, 'missing readings still cannot be confirmed');
    if (outcome === 'PASS' || outcome === 'NO_APPLICABLE_RULE') assert.doesNotMatch(detail, /finding-card__reviewer-actions/);
    const standalone = renderToStaticMarkup(createElement(FindingCard, props));
    assert.ok(standalone.includes(`class="finding-card__header" aria-expanded="${outcome === 'FAIL'}"`), 'standalone default is unchanged');
  }
  assert.equal(actions, 0, 'opening details never records a decision');
  // The backend's own reason should be visible without opening its raw trace. Never derive an
  // explanation from a rule id, normalize its numbers, or render drawing/model text as HTML.
  for (const outcome of ['NOT_FOUND', 'REVIEW_REQUIRED', 'NO_APPLICABLE_RULE']) {
    const reason = 'Recorded fixture reason: missing width <shop> & 9007199254740993/7 in.\nReview the source.';
    const abstention = { ...chain, outcome, operands: [], trace: { kind: 'abstention', cause: 'missing_input', reason } };
    const displayed = withChain(toFinding({ ...row(outcome), outcome }), abstention);
    assert.equal(displayed.reason, reason, 'reason is verbatim, including exact numeric text and line breaks');
    assert.equal(displayed.outcome, outcome);
    assert.strictEqual(displayed.recorded_chain, abstention, 'the original chain remains intact');
    assert.equal(displayed.trace.comparison, reason, 'raw trace is retained, not replaced');
    const html = renderToStaticMarkup(createElement(FindingCard, { ...cardProps, finding: displayed, defaultExpanded: true }));
    assert.ok(html.indexOf('finding-card__reason') < html.indexOf('finding-card__trace-section'), 'reason precedes the collapsed raw trace');
    assert.match(html, /missing width &lt;shop&gt; &amp; 9007199254740993\/7 in\./);
    assert.doesNotMatch(html, /<shop>/);
    assert.match(html, />Recorded trace<\/span>/);
    assert.match(html, /class="finding-card__trace-toggle" aria-expanded="false"/, 'trace disclosure announces its state');
    assert.doesNotMatch(html, />Calculation trace<\/span>/, 'an abstention is not arithmetic');
    const cleared = withChain(displayed, chain);
    assert.equal(cleared.reason, undefined, 'calculation refresh cannot keep an old abstention reason');
    const emptyReason = withChain(displayed, { ...abstention, trace: { kind: 'abstention', cause: 'missing_input', reason: '' } });
    assert.equal(emptyReason.reason, '', 'no fallback reason invented for an empty server field');
    assert.doesNotMatch(renderToStaticMarkup(createElement(FindingCard, { ...cardProps, finding: emptyReason, defaultExpanded: true })), /class="finding-card__reason"/);
    const opaque = withChain(displayed, { ...abstention, trace: { kind: 'unrecognised', content: { reason: 'Do not promote an unknown shape' } } });
    assert.equal(opaque.reason, undefined, 'unknown trace content is not guessed into a reason');
  }
  assert.equal(finding.reason, undefined, 'calculation does not acquire a generated explanation');
  assert.equal(finding.recorded_operands[0].value, '25 1/4 in');
  assert.equal(finding.recorded_operands[1].value, '9007199254740993 in');
  assert.equal(finding.recorded_operands[2].value, '2/3 in');
  assert.equal(finding.trace.operands[0].value, '101/4 in', 'persisted trace remains verbatim');
  assert.equal(finding.trace.comparison, '101/4 != 51/2');
  assert.strictEqual(finding.recorded_chain, chain, 'entire source record remains accessible');
  assert.equal(finding.evidence.length, 3, 'all locations remain, including repeated roles and unknown roles');
  assert.equal(finding.evidence[0].recorded_location.polygon[0][0], '9007199254740993.125');
  assert.equal(finding.evidence[0].page, 13, 'human page number is one-based exactly once');

  const markup = renderToStaticMarkup(createElement(EvidencePanel, { finding, projectId: 'project', packageId: 'package', onClose() {} }));
  assert.equal((markup.match(/Loading recorded drawing crop/g) ?? []).length, 3);
  assert.match(markup, /25 1\/4 in/);
  assert.match(markup, /Recorded drawing \(REFERENCE\)/);
  assert.match(markup, /9007199254740993.125/);
  assert.doesNotMatch(markup, /Interactive document viewer|Full context view/);

  const requests = [];
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    if (String(url).endsWith('/one/chain')) return Response.json(chain);
    if (String(url).endsWith('/two/chain')) return new Response('unavailable', { status: 503 });
    if (String(url).includes('cursor=second')) return Response.json({ items: [row('two')], next_cursor: null });
    return Response.json({ items: [row('one')], next_cursor: 'second' });
  };
  const all = await loadFindings('project', 'package');
  assert.deepEqual(all.map((item) => item.id), ['one', 'two'], 'short pages never truncate recorded findings');
  assert.equal(all[1].outcome, 'FAIL', 'failed chain lookup never removes a finding');
  assert.equal(all[1].recorded_operands, undefined, 'unavailable details do not become empty facts');
  assert.ok(requests.some((url) => url.includes('cursor=second')));

  globalThis.fetch = async () => Response.json({ items: [], next_cursor: 'loop' });
  await assert.rejects(loadFindings('project', 'package'), /fully loaded/, 'broken pagination fails clearly instead of claiming a complete list');
  console.log('evidence-data: exact values, all evidence, honest rendering, pagination and partial failures passed');
} finally {
  globalThis.fetch = originalFetch;
  await server.close();
}
