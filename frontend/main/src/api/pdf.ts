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

/**
 * One page of a PDF drawn to a picture (a data URL), `width` pixels wide: the cloud drawing card's
 * preview (#1522). It asks the server for byte ranges only (`disableAutoFetch`), so a large file is
 * not downloaded whole to draw one page. Throws when the file cannot be read; the card then keeps
 * its plain icon.
 */
export async function renderPdfPage(url: string, pageNumber: number, width: number, { byLabel = false }: { byLabel?: boolean } = {}): Promise<string> {
  const task = pdfjs.getDocument({ url, disableAutoFetch: true, disableStream: true, rangeChunkSize: 1 << 20 });
  const document = await task.promise;
  try {
    let index = Math.round(pageNumber);
    if (byLabel) {
      // #1581: the cloud drawing starts with a cover and labels the vendor's sheets with their own
      // numbers ("12" is the vendor's page 12); a file without labels keeps the plain number.
      const labels = await document.getPageLabels().catch(() => null);
      const found = labels ? labels.indexOf(String(index)) : -1;
      if (found >= 0) index = found + 1;
    }
    const page = await document.getPage(Math.min(Math.max(1, index), document.numPages));
    const base = page.getViewport({ scale: 1 });
    const viewport = page.getViewport({ scale: width / base.width });
    const canvas = window.document.createElement('canvas');
    canvas.width = Math.round(viewport.width);
    canvas.height = Math.round(viewport.height);
    await page.render({ canvas, viewport }).promise;
    return canvas.toDataURL('image/png');
  } finally {
    await task.destroy();
  }
}
