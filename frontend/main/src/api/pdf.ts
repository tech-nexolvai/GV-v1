/**
 * Reading a PDF far enough to count its pages.
 *
 * The API asks the client how many pages a file has, and is explicit that it does not check: *"Your
 * count, not ours — the API never opens the file. Ingestion builds the real page manifest and a
 * disagreement shows up there."*
 *
 * So the number has to be **true**, not plausible. A guess would be recorded, then contradicted by
 * ingestion, and the disagreement that exists to catch a corrupt upload would instead be pointing at
 * the browser. Counting `/Type /Page` in the raw bytes was the cheap option and is wrong on any PDF
 * using compressed object streams, which is most of them — so this parses properly.
 *
 * PDF.js is the frontend's stated viewer (`#150`: "React/Vite + TypeScript + PDF.js"), and the
 * reviewer workspace needs it to show a drawing beside a finding. Counting pages is its first use
 * rather than a dependency taken on for this alone.
 */

import * as pdfjs from 'pdfjs-dist';

// The worker is bundled rather than fetched from a CDN: a strict deployment blocks the CDN, and a
// viewer that silently fails to load its worker looks like a broken drawing rather than a missing
// asset.
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.mjs',
  import.meta.url,
).toString();

export class NotAReadablePdf extends Error {}

/**
 * How many pages the file has.
 *
 * Refuses rather than returning a fallback. A file this cannot open is one the pipeline cannot read
 * either, and finding that out now — with the reviewer still looking at the upload dialog — is far
 * better than recording a document whose ingestion fails an hour later.
 */
export async function countPdfPages(file: File): Promise<number> {
  try {
    const data = new Uint8Array(await file.arrayBuffer());
    const document = await pdfjs.getDocument({ data }).promise;
    const pages = document.numPages;
    await document.cleanup();

    if (!Number.isInteger(pages) || pages < 1) {
      throw new NotAReadablePdf(`${file.name} reports ${pages} pages, which is not a page count.`);
    }
    return pages;
  } catch (error) {
    if (error instanceof NotAReadablePdf) throw error;
    throw new NotAReadablePdf(
      `${file.name} could not be read as a PDF, so its page count is unknown. ` +
        'It has not been uploaded — a file this cannot open is one the pipeline cannot read either.',
    );
  }
}
