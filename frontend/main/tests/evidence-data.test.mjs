import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

// Use Vite's real module loader (including import.meta.env and CSS) without listening on a port.
// Every request is intercepted: no live package, stored evidence or backend data is changed.
const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
const originalFetch = globalThis.fetch;
try {
  const { toFinding, withChain, loadFindings } = await server.ssrLoadModule('/src/api/findings.ts');
  const { EvidencePanel } = await server.ssrLoadModule('/src/components/chat/EvidencePanel.tsx');
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
