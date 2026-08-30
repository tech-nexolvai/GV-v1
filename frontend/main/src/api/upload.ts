/**
 * Creating a package and getting drawings into storage.
 *
 * **The bytes never go through the API.** Registration returns a ticket — a URL scoped to one
 * storage key, permitting a write and nothing else, valid for a bounded time — and this client PUTs
 * the file straight to storage. `DESIGN_PLATFORM.md` §4.1: the control plane does short work only,
 * and on one 8 GB VM a request carrying a drawing competes with PostgreSQL and OCR for memory. The
 * API has an enumerating test that fails if any route grows a file body; this is the other half of
 * that arrangement.
 *
 * The order matters and each step depends on the last:
 *
 * 1. hash the file **in the browser** — registration is keyed on the digest, so it has to be known
 *    before the document exists
 * 2. register the document, receiving the ticket
 * 3. PUT the bytes to the ticket's URL, replaying its headers exactly
 * 4. confirm — the server reads the stored object back, hashes it, and refuses a mismatch
 *
 * Step 4 is the one that makes the rest trustworthy. The API does not take "it uploaded" on trust,
 * so a truncated or swapped file is refused rather than recorded.
 */

import { openReviewSession } from './client';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

/** SHA-256 of a file, lowercase hex — the form the API's `^[0-9a-f]{64}$` pattern expects. */
export async function sha256(file: File): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

interface Ticket {
  document_id: string;
  storage_key: string;
  upload_url: string;
  method: string;
  expires_at: string;
  required_headers: Record<string, string>;
}

/**
 * Where the bytes actually go.
 *
 * On the S3 backend the ticket is a presigned HTTPS URL and this returns it unchanged — the browser
 * writes straight to the bucket, which is the whole point of a ticket.
 *
 * On the local filesystem backend it is a `file:` URI with the signed token in the query string, and
 * **a browser cannot PUT to `file:`**. `scripts/dev_server.py` mounts a shim that verifies the same
 * token and performs the write; this points at it. The shim exists only in that script, never in
 * `create_app`, so the API keeps its guarantee that no route it serves accepts a file body.
 *
 * Decided from the URL rather than from a build flag: the client does what the ticket it was handed
 * permits, so nothing here has to know which deployment it is talking to.
 */
function writableUrl(ticket: Ticket): string {
  if (!ticket.upload_url.startsWith('file:')) return ticket.upload_url;

  // The whole query string is carried over, not a parameter picked out by name: the store composes
  // that URL and only the store decides what the ticket is called. Copying it verbatim means a
  // rename on the Python side cannot silently strip the ticket and turn every upload into a 403.
  const source = new URL(ticket.upload_url);
  const base = BASE.replace(/\/api\/v1$/, '');
  return `${base}/_dev/upload/${ticket.storage_key}${source.search}`;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${detail.slice(0, 400)}`);
  }
  return (await response.json()) as T;
}

export type DocumentKind = 'architectural' | 'shop' | 'schedule' | 'product_spec';

export interface UploadProgress {
  step: string;
  file?: string;
}

/**
 * Put one drawing into storage and confirm it, returning the document version.
 *
 * `onProgress` exists because this is four round trips and a file transfer: a modal that says
 * nothing for twenty seconds is one a reviewer will click twice.
 */
export async function uploadDocument(
  projectId: string,
  packageId: string,
  file: File,
  kind: DocumentKind,
  onProgress?: (progress: UploadProgress) => void,
): Promise<{ document_id: string }> {
  onProgress?.({ step: 'Hashing', file: file.name });
  const digest = await sha256(file);

  onProgress?.({ step: 'Registering', file: file.name });
  const ticket = await post<Ticket>(
    `/projects/${projectId}/packages/${packageId}/documents`,
    { kind, sha256: digest },
  );

  onProgress?.({ step: 'Uploading', file: file.name });
  const written = await fetch(writableUrl(ticket), {
    method: ticket.method,
    // Replayed exactly. A signature normally covers them, so the same bytes sent with a different
    // verb or content type is a different request and a signing backend will refuse it.
    headers: ticket.required_headers,
    body: file,
  });
  if (!written.ok) {
    throw new Error(
      `The drawing could not be written to storage (${written.status}). Nothing has been recorded.`,
    );
  }

  onProgress?.({ step: 'Confirming', file: file.name });
  // The page count is read from the PDF rather than guessed. The API states plainly that it does not
  // open the file and that ingestion builds the real manifest, so a disagreement surfaces there — a
  // fabricated number would manufacture one.
  // Imported here rather than at module scope. PDF.js is large, and most sessions are a reviewer
  // reading findings rather than uploading a drawing — loading a parser for all of them to serve the
  // few that need it makes every page slower for no one's benefit.
  const { countPdfPages } = await import('./pdf');
  const pageCount = await countPdfPages(file);
  await post(`/projects/${projectId}/documents/${ticket.document_id}/confirm`, {
    sha256: digest,
    page_count: pageCount,
  });

  return { document_id: ticket.document_id };
}

export interface NewPackage {
  vendor: string;
  architectural?: File | null;
  shop?: File | null;
}

/**
 * Create a package, upload whatever drawings were chosen, and open a review session over it.
 *
 * Documents are uploaded in sequence rather than in parallel: each is four round trips plus a file
 * transfer, and two at once on a poor connection makes both slower and the progress meaningless.
 */
export async function createPackage(
  projectId: string,
  input: NewPackage,
  onProgress?: (progress: UploadProgress) => void,
): Promise<{ packageId: string; reviewSessionId: string | null }> {
  onProgress?.({ step: 'Creating document set' });
  const created = await post<{ id: string; current_revision_id: string }>(
    `/projects/${projectId}/packages`,
    { vendor: input.vendor || null },
  );

  const files: Array<[File, DocumentKind]> = [];
  if (input.architectural) files.push([input.architectural, 'architectural']);
  if (input.shop) files.push([input.shop, 'shop']);

  for (const [file, kind] of files) {
    await uploadDocument(projectId, created.id, file, kind, onProgress);
  }

  onProgress?.({ step: 'Opening review' });
  const session = await openReviewSession(projectId, created.id, created.current_revision_id);

  return { packageId: created.id, reviewSessionId: session.id };
}
