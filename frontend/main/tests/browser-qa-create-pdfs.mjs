/** Generate genuine non-client PDFs for a browser's native file chooser. */
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fixturePdf } from './browser-qa-scenarios.mjs';

const directory = mkdtempSync(join(tmpdir(), 'gv-frontend-qa-'));
for (const role of ['architectural', 'shop']) {
  const path = join(directory, `${role}-synthetic.pdf`);
  writeFileSync(path, fixturePdf(role));
  console.log(path);
}
