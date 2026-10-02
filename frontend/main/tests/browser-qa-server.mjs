/**
 * Isolated visual QA of the actual application. Run: node tests/browser-qa-server.mjs
 * No proxy and no backend connection. All /api and /_dev requests end in this middleware.
 * Unknown/mutating requests are refused, never forwarded or recorded as real actions.
 */
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { project, populated, empty, packages, findings, chains, needed, candidates, cropPng, samplePdf, sampleWorkbook } from './browser-qa-fixtures.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const failCropOnce = new Set();
let cropRetryMode = false;
let approvedMode = false;
const qa = {
  name: 'isolated-synthetic-ui-qa',
  transformIndexHtml(html) {
    return html.replace('<body>', '<body><div style="position:fixed;z-index:10000;bottom:6px;left:8px;padding:5px 9px;background:#66172b;color:white;border-radius:5px;font:10px monospace;pointer-events:none">SYNTHETIC UI QA · NO BACKEND CONNECTION</div>');
  },
  configureServer(server) {
    server.middlewares.use(async (req, res, next) => {
      const url = new URL(req.url, 'http://127.0.0.1:5193');
      // Set explicit isolated-server modes from the document navigation. Some browsers omit
      // Referer on blob requests, so evidence behavior must not depend on that header.
      if (url.pathname === '/' || url.pathname === '/index.html') {
        cropRetryMode = url.searchParams.get('crop-error') === 'once';
        approvedMode = url.searchParams.get('approved') === '1';
        failCropOnce.clear();
      }
      if (!url.pathname.startsWith('/api') && !url.pathname.startsWith('/_dev')) return next();
      const path = url.pathname;
      const visiblePackages = packages.map((item) => approvedMode && item.id === populated ? { ...item, state: 'APPROVED' } : item);
      const pkg = visiblePackages.find((item) => path.includes(`/packages/${item.id}`));
      const rows = pkg?.id === empty ? [] : findings.map((row) => approvedMode ? {
        ...row, reviewer_action: { action: 'CONFIRM', actor: 'Synthetic QA reviewer' },
      } : row);
      const json = (body, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(body)); };
      const refusal = (message, status = 404) => json({ error: 'synthetic_qa_only', message, request_id: 'SYNTHETIC_UI_QA' }, status);
      if (req.method === 'POST' && /\/chat(?:\/stream)?$/.test(path)) {
        let body = ''; for await (const part of req) body += part;
        const { question = '' } = JSON.parse(body || '{}');
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
      if (path.endsWith('/required-inputs')) return json(needed);
      if (path.endsWith('/candidates')) return json({ candidates, total: candidates.length });
      if (path.endsWith('/views')) return json({ views: [], total: 0 });
      if (path.endsWith('/findings/summary')) return json({ total: rows.length, failed: rows.length ? 1 : 0, passed: rows.length ? 1 : 0, review_required: rows.length ? 1 : 0, not_found: rows.length ? 1 : 0, no_applicable_rule: 0, critical_failed: 0 });
      if (path.endsWith('/findings')) return json({ items: rows, next_cursor: null, limit: 50, ordering: 'synthetic-test-order' });
      if (path.endsWith('/chain')) { const id = path.split('/').at(-2); return chains[id] ? json(chains[id]) : refusal('Unknown synthetic finding.'); }
      if (path.endsWith('/crop')) {
        // Optional query enables an isolated retry check without changing the actual app.
        if (cropRetryMode && !failCropOnce.has(path)) { failCropOnce.add(path); return refusal('Synthetic QA: temporary crop failure. Retry is safe.', 503); }
        res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' }); res.end(cropPng(path.includes('shop'))); return;
      }
      if (/\/(report|redline)\.pdf$/.test(path)) { res.writeHead(200, { 'Content-Type': 'application/pdf', 'Content-Disposition': 'attachment; filename="synthetic-ui-qa.pdf"' }); res.end(samplePdf(path.endsWith('redline.pdf'))); return; }
      if (path.endsWith('/report')) { res.writeHead(200, { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'Content-Disposition': 'attachment; filename="synthetic-ui-qa.xlsx"' }); res.end(sampleWorkbook()); return; }
      if (pkg && path.endsWith(`/packages/${pkg.id}`)) return json(pkg);
      return refusal(`No synthetic fixture for ${req.method} ${path}. No live backend was contacted.`);
    });
  },
};
const server = await createServer({
  configFile: false, root, envDir: false, cacheDir: 'node_modules/.vite-synthetic-qa', plugins: [react(), qa],
  define: { 'import.meta.env.VITE_PROJECT_ID': JSON.stringify(project), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api/v1') },
  server: { host: '127.0.0.1', port: 5193, strictPort: true, proxy: {} },
});
await server.listen();
console.log(`SYNTHETIC UI QA ONLY — no proxy, no backend connection\nPopulated: http://127.0.0.1:5193/#/review/${populated}\nEmpty: http://127.0.0.1:5193/#/review/${empty}\nCrop retry: http://127.0.0.1:5193/?crop-error=once#/review/${populated}\nApproved/downloads: http://127.0.0.1:5193/?approved=1#/review/${populated}`);
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => { await server.close(); process.exit(0); });
