import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

// Exercise the real upload orchestration and API client. Only PDF parsing and the network are
// replaced; a request not explicitly listed below fails, and no HTTP listener is started.
const root = fileURLToPath(new URL('../', import.meta.url));
const originalFetch = globalThis.fetch;
const pdfReads = [];
let invalidPdf = null;
globalThis.__uploadTestCountPages = async (file) => {
  pdfReads.push(file.name);
  if (file.name === invalidPdf) throw new Error('Synthetic corrupt PDF');
  return file.name === 'architect.pdf' ? 2 : 3;
};

const server = await createServer({
  root,
  configFile: false,
  envDir: false,
  logLevel: 'silent',
  appType: 'custom',
  server: { middlewareMode: true },
  define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api/v1') },
  plugins: [{
    name: 'upload-test-pdf-parser',
    enforce: 'pre',
    resolveId(id, importer) {
      if (id === './pdf' && importer?.endsWith('/src/api/upload.ts')) return '\0test-pdf-parser';
    },
    load(id) {
      if (id === '\0test-pdf-parser') {
        return 'export const countPdfPages = (file) => globalThis.__uploadTestCountPages(file);';
      }
    },
  }],
});

try {
  const { createPackage } = await server.ssrLoadModule('/src/api/upload.ts');
  const { PartialUploadError } = await server.ssrLoadModule('/src/api/uploadState.ts');
  const arch = new File(['synthetic architectural bytes'], 'architect.pdf', { type: 'application/pdf' });
  const shop = new File(['synthetic vendor bytes'], 'shop.pdf', { type: 'application/pdf' });
  const project = '/api/v1/projects/test-project';
  const pkg = `${project}/packages/saved-package`;

  function network(failAt = null) {
    const calls = [];
    let registered = 0;
    globalThis.fetch = async (url, init = {}) => {
      const call = { url: String(url), method: init.method ?? 'GET', body: init.body, headers: init.headers };
      calls.push(call);
      if (call.url === failAt) return new Response('Synthetic service failure', { status: 503 });
      if (call.url === `${project}/packages`) {
        assert.equal(call.method, 'POST');
        assert.deepEqual(JSON.parse(call.body), { vendor: 'Synthetic test vendor' });
        return Response.json({ id: 'saved-package', current_revision_id: 'revision-1' });
      }
      if (call.url === `${pkg}/documents`) {
        registered += 1;
        const file = registered === 1 ? arch : shop;
        assert.deepEqual(JSON.parse(call.body), {
          kind: registered === 1 ? 'architectural' : 'shop',
          sha256: createHash('sha256').update(await file.text()).digest('hex'),
        });
        return Response.json({
          document_id: `document-${registered}`,
          storage_key: `drawing-${registered}`,
          upload_url: `file:///synthetic/drawing-${registered}?signed=synthetic-token`,
          method: 'PUT',
          required_headers: { 'Content-Type': 'application/pdf', 'X-Test-Signature': `signature-${registered}` },
        });
      }
      if (call.url.startsWith('/_dev/upload/')) {
        assert.equal(call.method, 'PUT');
        assert.equal(call.body, registered === 1 ? arch : shop, 'the original file bytes reach storage');
        assert.deepEqual(call.headers, { 'Content-Type': 'application/pdf', 'X-Test-Signature': `signature-${registered}` });
        return new Response(null, { status: 200 });
      }
      if (call.url === `${project}/documents/document-${registered}/confirm`) {
        const file = registered === 1 ? arch : shop;
        assert.deepEqual(JSON.parse(call.body), {
          sha256: createHash('sha256').update(await file.text()).digest('hex'),
          page_count: registered === 1 ? 2 : 3,
        });
        return Response.json({ document_id: `document-${registered}` });
      }
      if (call.url === `${pkg}/extract`) return Response.json({ accepted_id: 'accepted-1' });
      if (call.url === `${pkg}/review-sessions`) {
        assert.deepEqual(JSON.parse(call.body), { package_revision_id: 'revision-1' });
        return Response.json({ id: 'session-1' });
      }
      throw new Error(`Unexpected request in upload test: ${call.method} ${call.url}`);
    };
    return calls;
  }

  // The first PDF is fully confirmed before the second storage write fails. Its record must stay.
  let calls = network('/_dev/upload/drawing-2?signed=synthetic-token');
  await assert.rejects(createPackage('test-project', { vendor: 'Synthetic test vendor', architectural: arch, shop }), (error) => {
    assert.ok(error instanceof PartialUploadError);
    assert.equal(error.packageId, 'saved-package');
    assert.match(error.message, /503/);
    return true;
  });
  assert.equal(calls.filter((call) => call.url === `${project}/packages`).length, 1, 'never creates a replacement package');
  assert.equal(calls.filter((call) => call.url.endsWith('/confirm')).length, 1, 'the successful first confirmation is retained');
  assert.ok(calls.every((call) => call.method !== 'DELETE'), 'failure never deletes a saved record');
  assert.ok(!calls.some((call) => call.url.endsWith('/extract') || call.url.endsWith('/review-sessions')));

  // A corrupt second PDF is refused before creating even the package.
  calls = network();
  invalidPdf = 'shop.pdf';
  await assert.rejects(createPackage('test-project', { vendor: 'Synthetic test vendor', architectural: arch, shop }), /Synthetic corrupt PDF/);
  assert.deepEqual(calls, []);
  invalidPdf = null;

  // Success keeps the production order, ticket headers, hashes, roles, and revision/session link.
  calls = network();
  pdfReads.length = 0;
  const progress = [];
  const result = await createPackage('test-project', {
    vendor: 'Synthetic test vendor', architectural: arch, shop,
  }, (step) => progress.push(step.step));
  assert.deepEqual(result, { packageId: 'saved-package', reviewSessionId: 'session-1' });
  assert.deepEqual(calls.map((call) => `${call.method} ${call.url}`), [
    `POST ${project}/packages`,
    `POST ${pkg}/documents`,
    'PUT /_dev/upload/drawing-1?signed=synthetic-token',
    `POST ${project}/documents/document-1/confirm`,
    `POST ${pkg}/documents`,
    'PUT /_dev/upload/drawing-2?signed=synthetic-token',
    `POST ${project}/documents/document-2/confirm`,
    `POST ${pkg}/extract`,
    `POST ${pkg}/review-sessions`,
  ]);
  assert.deepEqual(pdfReads, ['architect.pdf', 'shop.pdf', 'architect.pdf', 'shop.pdf']);
  assert.equal(progress[0], 'Validating PDF files');
  assert.equal(progress.at(-1), 'Opening review');

  // Extraction/session failures also retain the existing package and both confirmed uploads.
  for (const failedEndpoint of [`${pkg}/extract`, `${pkg}/review-sessions`]) {
    calls = network(failedEndpoint);
    await assert.rejects(createPackage('test-project', { vendor: 'Synthetic test vendor', architectural: arch, shop }), (error) => {
      assert.ok(error instanceof PartialUploadError);
      assert.equal(error.packageId, 'saved-package');
      return true;
    });
    assert.equal(calls.filter((call) => call.url === `${project}/packages`).length, 1);
    assert.equal(calls.filter((call) => call.url.endsWith('/confirm')).length, 2);
    assert.ok(calls.every((call) => call.method !== 'DELETE'));
  }
  console.log('upload flow: partial upload, prevalidation, successful contracts, and post-upload failures passed (no live requests)');
} finally {
  globalThis.fetch = originalFetch;
  delete globalThis.__uploadTestCountPages;
  await server.close();
}
