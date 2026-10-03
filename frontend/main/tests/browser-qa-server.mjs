/**
 * Isolated visual QA of the actual application. Run: node tests/browser-qa-server.mjs
 * No proxy and no backend connection. All /api and /_dev requests end in this middleware.
 * Unknown writes are refused. Explicit QA scenarios use memory only, never a backend.
 */
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { project, populated, empty, packages, findings, chains, needed, candidates, cropPng, samplePdf, sampleWorkbook } from './browser-qa-fixtures.mjs';
import { createScenarioState, handleScenario, scenarioSnapshot, scenarioPackages, scenarioFindings, fixturePdf, prepareUploadedFixture } from './browser-qa-scenarios.mjs';
import { pageFixture } from './browser-qa-pages.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const failCropOnce = new Set();
const settingCropAttempts = new Map();
let cropRetryMode = false;
let approvedMode = false;
let pageMode = 'populated';
let reportErrorMode = false;
let settingMode = false;
let scenario = createScenarioState();
const port = Number(process.env.GV_QA_PORT ?? '5193');
const qa = {
  name: 'isolated-synthetic-ui-qa',
  transformIndexHtml(html) {
    return html.replace('<body>', '<body><div style="position:fixed;z-index:10000;bottom:6px;left:8px;padding:5px 9px;background:#66172b;color:white;border-radius:5px;font:10px monospace;pointer-events:none">SYNTHETIC UI QA · NO BACKEND CONNECTION</div>');
  },
  configureServer(server) {
    server.middlewares.use(async (req, res, next) => {
      const url = new URL(req.url, `http://127.0.0.1:${port}`);
      // Set explicit isolated-server modes from the document navigation. Some browsers omit
      // Referer on blob requests, so evidence behavior must not depend on that header.
      if (url.pathname === '/' || url.pathname === '/index.html') {
        cropRetryMode = url.searchParams.get('crop-error') === 'once';
        approvedMode = url.searchParams.get('approved') === '1';
        pageMode = url.searchParams.get('pages') ?? 'populated';
        reportErrorMode = url.searchParams.get('report-error') === '1';
        settingMode = url.searchParams.get('settings') === '1';
        failCropOnce.clear();
        settingCropAttempts.clear();
        const requestedScenario = url.searchParams.get('scenario');
        if (requestedScenario !== scenario.name || url.searchParams.get('reset') === '1') {
          scenario = createScenarioState(requestedScenario);
          if (scenario.name === 'review-flow' && url.searchParams.get('fixture-uploaded') === '1') prepareUploadedFixture(scenario);
        }
      }
      if (url.pathname === '/__qa') {
        res.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' });
        res.end(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synthetic QA event log</title><style>body{max-width:1000px;margin:40px auto;padding:0 20px;font:15px/1.6 system-ui;color:#252020;background:#fcfaf9}h1{color:#66172b}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:24px;background:#f2ecee;border:1px solid #dbcbd0;border-radius:12px}a{color:#66172b}</style><h1>Synthetic frontend QA · Event log</h1><p>This page shows only the isolated harness's in-memory state. No backend, database, credentials or client data is connected. Refreshing this log does not reset a scenario.</p><p><a href="/?scenario=partial-upload#/"><strong>Partial-upload scenario</strong></a> · <a href="/?scenario=approval#/review/${populated}"><strong>Approval scenario</strong></a></p><p role="status" id="status">Loading local test state…</p><pre id="state"></pre><script>async function refresh(){try{const response=await fetch('/__qa/state');const state=await response.json();document.querySelector('#status').textContent='Scenario: '+(state.scenario||'baseline')+' · Created packages: '+state.packageCreates+' · Confirmed PDFs: '+state.confirmedDocuments+' · Reviewed: '+state.reviewedCount+'/'+state.findingCount+' · Approved: '+state.approved;document.querySelector('#state').textContent=JSON.stringify(state,null,2);}catch(error){document.querySelector('#status').textContent=error.message;}}refresh();setInterval(refresh,1000);</script></html>`);
        return;
      }
      if (url.pathname.startsWith('/__qa/')) {
        if (req.method !== 'GET') { res.writeHead(405); res.end('Synthetic QA controls are read-only.'); return; }
        if (url.pathname === '/__qa/state') { res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(scenarioSnapshot(scenario))); return; }
        if (/^\/__qa\/pdf\/(architectural|shop)$/.test(url.pathname)) { res.writeHead(200, { 'Content-Type': 'application/pdf' }); res.end(fixturePdf(url.pathname.split('/').at(-1))); return; }
        res.writeHead(404); res.end('Unknown synthetic fixture.'); return;
      }
      if (!url.pathname.startsWith('/api') && !url.pathname.startsWith('/_dev')) return next();
      const path = url.pathname;
      const visiblePackages = (scenario.name ? scenarioPackages(scenario) : packages).map((item) => approvedMode && item.id === populated ? { ...item, state: 'APPROVED' } : item);
      const pkg = visiblePackages.find((item) => path.includes(`/packages/${item.id}`));
      const rows = (scenario.name ? scenarioFindings(scenario, pkg?.id) : pkg?.id === empty ? [] : findings).map((row) => approvedMode ? {
        ...row, reviewer_action: { action: 'confirm', actor: 'Synthetic QA reviewer' },
      } : row);
      const json = (body, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(body)); };
      const refusal = (message, status = 404) => json({ error: 'synthetic_qa_only', message, request_id: 'SYNTHETIC_UI_QA' }, status);
      const pageResponse = req.method === 'GET' ? pageFixture(path, pageMode, url.searchParams.get('cursor')) : null;
      if (pageResponse) return json(pageResponse.body, pageResponse.status);
      let requestBody = {};
      if (!['GET', 'HEAD'].includes(req.method)) {
        const parts = []; let size = 0;
        for await (const part of req) { size += part.length; if (size > 1024 * 1024) return refusal('Synthetic QA only accepts fixture bodies below 1 MiB.', 413); parts.push(part); }
        const bytes = Buffer.concat(parts);
        if (req.method === 'PUT') requestBody = bytes;
        else { try { requestBody = bytes.length ? JSON.parse(bytes.toString()) : {}; } catch { return refusal('Invalid synthetic request JSON.', 400); } }
      }
      const localResponse = handleScenario(scenario, req.method, path, requestBody);
      if (localResponse) {
        if (scenario.name === 'reading-confirmation' && req.method === 'POST' && path.endsWith('/confirm')) await new Promise(resolve => setTimeout(resolve, 1500));
        if (scenario.name === 'action-save' && req.method === 'POST' && path.endsWith('/actions')) await new Promise(resolve => setTimeout(resolve, requestBody.action === 'dismiss' ? 12000 : 1500));
        // Deliberate delay only in isolated QA: makes the pending/disabled state inspectable.
        if (scenario.name === 'decision-save' && req.method === 'POST' && /\/(evidence|exceptions)$/.test(path)) await new Promise(resolve => setTimeout(resolve, 1500));
        return json(localResponse.body, localResponse.status);
      }
      if (req.method === 'POST' && /\/chat(?:\/stream)?$/.test(path)) {
        const { question = '' } = requestBody;
        scenario.events.push({ sequence: scenario.events.length + 1, method: req.method, path, status: 200, result: 'Synthetic structured fallback; no model connection', question });
        const selected = /fail/i.test(question) ? rows.filter((row) => row.outcome === 'FAIL') : rows;
        const answer = `Synthetic UI QA: showing ${selected.length} of ${rows.length} fixture findings. This is a frontend test, not a drawing review.`;
        const reply = { mode: 'structured_fallback', model_id: null, fallback_reason: 'Synthetic QA provider deliberately disabled', answer, summary: null, total: rows.length,
          findings: selected.map((row) => ({ finding_id: row.id, text: `${row.rule_id}: ${row.outcome}. Synthetic test record.` })) };
        if (path.endsWith('/stream')) {
          res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
          res.write(`event: facts\ndata: ${JSON.stringify({ answer, finding_ids: selected.map((row) => row.id), total: rows.length, narrating: false })}\n\n`);
          res.end(`event: narration\ndata: ${JSON.stringify(reply)}\n\n`);
        } else json(reply);
        return;
      }
      if (req.method !== 'GET') return refusal('This isolated QA harness does not save or run reviews. No backend request was made.', 409);
      if (path.endsWith('/review-sessions')) return json({ items: [], next_cursor: null, limit: 50 });
      if (path.endsWith('/packages')) return json({ items: visiblePackages, next_cursor: null, limit: 50, ordering: 'created_at' });
      if (path.endsWith('/chat/models')) return json({ models: [], default: null });
      if (path.endsWith('/semantic-types')) return json(['countertop_depth', 'filler_width']);
      if (path.endsWith('/required-inputs')) return json(settingMode ? { ...needed, parameters: [
        { name: 'countertop_overhang', scope: 'project', rule_ids: ['CT-WIDTH-001'], blocked: false, declared_default: null,
          sources: [{ value: 'G.C / Client', guidance: 'Synthetic source guidance.' }],
          found: { proposal_id: 'synthetic-setting-passage', page_index: 0, document_kind: 'architectural', has_crop: true } },
      ] } : needed);
      if (path.endsWith('/candidates')) return json({ candidates, total: candidates.length });
      if (path.endsWith('/views')) return json({ views: [], total: 0 });
      if (path.endsWith('/findings/summary')) return json({ total: rows.length, failed: rows.length ? 1 : 0, passed: rows.length ? 1 : 0, review_required: rows.length ? 1 : 0, not_found: rows.length ? 1 : 0, no_applicable_rule: 0, critical_failed: 0 });
      if (path.endsWith('/findings')) return json({ items: rows, next_cursor: null, limit: 50, ordering: 'synthetic-test-order' });
      if (path.endsWith('/chain')) { const id = path.split('/').at(-2); return chains[id] ? json(chains[id]) : refusal('Unknown synthetic finding.'); }
      if (path.endsWith('/crop')) {
        if (settingMode && cropRetryMode && path.includes('/parameter-proposals/')) {
          const attempts = (settingCropAttempts.get(path) ?? 0) + 1;
          settingCropAttempts.set(path, attempts);
          // React StrictMode mounts twice; keep the initial failure visible until manual retry.
          if (attempts <= 2) return refusal('Synthetic passage crop temporarily unavailable.', 503);
        }
        // Optional query enables an isolated retry check without changing the actual app.
        if (cropRetryMode && !path.includes('/parameter-proposals/') && !failCropOnce.has(path)) { failCropOnce.add(path); return refusal('Synthetic QA: temporary crop failure. Retry is safe.', 503); }
        scenario.events.push({ sequence: scenario.events.length + 1, method: req.method, path, status: 200, result: 'Synthetic PNG served' });
        res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' }); res.end(cropPng(path.includes('shop'))); return;
      }
      if (reportErrorMode && /\/(report(?:\.pdf)?|redline\.pdf)$/.test(path)) return refusal('Synthetic report unavailable. Sign-off and findings are unchanged.', 503);
      if (/\/(report|redline)\.pdf$/.test(path)) {
        scenario.events.push({ sequence: scenario.events.length + 1, method: req.method, path, status: 200, result: 'Synthetic PDF served; not a backend export' });
        res.writeHead(200, { 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="synthetic-ui-qa.pdf"' }); res.end(samplePdf(path.endsWith('redline.pdf'))); return;
      }
      if (path.endsWith('/report')) {
        scenario.events.push({ sequence: scenario.events.length + 1, method: req.method, path, status: 200, result: 'Synthetic workbook served; not a backend export' });
        res.writeHead(200, { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'Content-Disposition': 'attachment; filename="synthetic-ui-qa.xlsx"' }); res.end(sampleWorkbook()); return;
      }
      if (pkg && path.endsWith(`/packages/${pkg.id}`)) return json(pkg);
      return refusal(`No synthetic fixture for ${req.method} ${path}. No live backend was contacted.`);
    });
  },
};
const server = await createServer({
  configFile: false, root, envDir: false, cacheDir: 'node_modules/.vite-synthetic-qa', plugins: [react(), qa],
  define: { 'import.meta.env.VITE_PROJECT_ID': JSON.stringify(project), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api/v1') },
  server: { host: '127.0.0.1', port, strictPort: true, proxy: {} },
});
await server.listen();
console.log(`SYNTHETIC UI QA ONLY — no proxy, no backend connection\nPopulated: http://127.0.0.1:${port}/#/review/${populated}\nEmpty: http://127.0.0.1:${port}/#/review/${empty}\nCrop retry: http://127.0.0.1:${port}/?crop-error=once#/review/${populated}\nApproved/downloads: http://127.0.0.1:${port}/?approved=1#/review/${populated}\nPartial upload: http://127.0.0.1:${port}/?scenario=partial-upload#/\nApproval flow: http://127.0.0.1:${port}/?scenario=approval#/review/${populated}`);
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => { await server.close(); process.exit(0); });
