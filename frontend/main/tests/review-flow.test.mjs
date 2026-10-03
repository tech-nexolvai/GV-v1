import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { project, populated, packages, findings } from './browser-qa-fixtures.mjs';
import { createScenarioState, handleScenario, scenarioSnapshot, fixturePdf, flowMeasurements } from './browser-qa-scenarios.mjs';

// Real frontend upload and API clients; a strict in-memory network. This proves UI contracts,
// not OCR, server parsing, verdict calculation, provider behavior or backend exports.
const originalFetch = globalThis.fetch;
const state = createScenarioState('review-flow');
const server = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)), configFile: false, envDir: false,
  logLevel: 'silent', appType: 'custom', server: { middlewareMode: true },
  define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api/v1') },
  plugins: [{ name: 'fixture-page-count', enforce: 'pre',
    resolveId(id, importer) { if (id === './pdf' && importer?.endsWith('/src/api/upload.ts')) return '\0fixture-page-count'; },
    load(id) { if (id === '\0fixture-page-count') return 'export const countPdfPages = async () => 1;'; },
  }],
});
try {
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method ?? 'GET';
    const body = init.body instanceof File ? Buffer.from(await init.body.arrayBuffer()) : init.body ? JSON.parse(init.body) : {};
    const result = handleScenario(state, method, String(url).split('?')[0], body);
    assert.ok(result, `Unexpected network request: ${method} ${url}`);
    return Response.json(result.body, { status: result.status });
  };
  const { createPackage } = await server.ssrLoadModule('/src/api/upload.ts');
  const api = await server.ssrLoadModule('/src/api/client.ts');
  const result = await createPackage(project, {
    vendor: 'Connected flow fixture',
    architectural: new File([fixturePdf('architectural')], 'synthetic-architect.pdf', { type: 'application/pdf' }),
    shop: new File([fixturePdf('shop')], 'synthetic-shop.pdf', { type: 'application/pdf' }),
  });
  const { packageId, reviewSessionId } = result;
  assert.equal(state.documents.filter((document) => document.confirmed).length, 2);
  assert.equal(state.sessions.length, 1);
  assert.deepEqual((await api.listFindings(project, packageId)).items, [], 'uploaded is not checked');
  assert.equal(scenarioSnapshot(state).findingCount, 0, 'the QA log must not count results before fixture checks');
  await assert.rejects(api.requestChecks(project, packageId), /Save fixture values/);
  const bad = flowMeasurements.map((measurement) => ({ ...measurement, value: '25' }));
  await assert.rejects(api.enterMeasurements(project, packageId, { measurements: bad }), /documented synthetic depths/);
  assert.equal(state.storedMeasurements, null);
  const receipt = await api.enterMeasurements(project, packageId, { measurements: flowMeasurements });
  assert.deepEqual(state.storedMeasurements, flowMeasurements, 'typed strings preserved exactly');
  assert.deepEqual(receipt.measurements.map(({ numerator, denominator }) => [numerator, denominator]), [['101', '4'], ['51', '2']]);
  assert.equal((await api.requestChecks(project, packageId)).accepted_id, 'synthetic-checks');
  const rows = (await api.listFindings(project, packageId)).items;
  assert.equal(rows.length, 4);
  assert.ok(rows.every((row) => row.package_revision_id === state.createdPackage.current_revision_id));
  assert.ok(rows.every((row) => !findings.some((existing) => existing.id === row.id)), 'separate packages do not share finding identities');
  const chain = await api.getFindingChain(project, packageId, rows[0].id);
  assert.equal(chain.finding_id, rows[0].id);
  assert.equal(chain.operands[0].numerator, '51');
  assert.ok(chain.operands[0].evidence.crop_uri);
  const prefix = `/api/v1/projects/${project}`;
  const approvalPath = `${prefix}/review-sessions/${reviewSessionId}/approve`;
  assert.equal(handleScenario(state, 'POST', approvalPath).status, 409);
  assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${packageId}/report.pdf`).status, 409);
  // A finding from the other fixture must not be accepted into this sitting.
  await assert.rejects(api.recordReviewAction(project, reviewSessionId, { finding_id: findings[0].id, action: 'confirm' }), /existing synthetic findings/);
  for (const row of rows) await api.recordReviewAction(project, reviewSessionId, { finding_id: row.id, action: 'confirm' });
  assert.equal(handleScenario(state, 'POST', approvalPath).status, 201);
  assert.equal((await api.getPackage(project, packageId)).state, 'APPROVED');
  assert.deepEqual((await api.listFindings(project, packageId)).items.map((row) => row.outcome), rows.map((row) => row.outcome));
  assert.deepEqual(await api.getPackage(project, populated), packages[0], 'existing fixture package is unchanged');
  assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${populated}/report.pdf`).status, 409, 'approval never unlocks another package');
  for (const artifact of ['report', 'report.pdf', 'redline.pdf']) {
    assert.equal(handleScenario(state, 'GET', `${prefix}/packages/${packageId}/${artifact}`), null, 'approved artifact passes to fixture bytes');
  }
  await assert.rejects(api.requestChecks(project, packageId), /signed-off fixtures are read-only/);
  assert.equal(handleScenario(state, 'DELETE', `${prefix}/packages/${packageId}`).status, 409);
  const events = scenarioSnapshot(state).events;
  assert.ok(events.findIndex((event) => event.path.endsWith('/extract')) > events.findLastIndex((event) => event.path.endsWith('/confirm')));
  assert.equal(state.checksRequests, 1);
  console.log('connected review flow: real frontend clients, two PDF uploads, exact-value transport, empty results, checks, chain, review actions, sign-off and artifact gating passed (in-memory API only)');
} finally {
  globalThis.fetch = originalFetch;
  await server.close();
}
