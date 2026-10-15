/**
 * The side-by-side viewer's data (#1355): the W7 pages listing (`GET .../pages`, #1342) and one
 * stored page picture at a time. Types come from the generated schema, so a renamed field stops this
 * compiling. Nothing here renders, reads or decides anything.
 */

import { ApiError, downloadPagePicture, type ErrorEnvelope } from '@/api/client';
import type { components } from '@/api/schema';

type Schemas = components['schemas'];

export type ReviewPagesOut = Schemas['ReviewPagesOut'];
export type ReviewDocument = Schemas['ReviewDocumentOut'];
export type ReviewPage = Schemas['ReviewPageOut'];
export type ArchitectRegion = Schemas['ArchitectRegionOut'];

/** The calls the viewer makes. The app uses `liveReviewPagesApi`; tests and screenshots pass their own. */
export interface ReviewPagesApi {
  loadPages: (projectId: string, packageId: string) => Promise<ReviewPagesOut>;
  loadPicture: (projectId: string, packageId: string, pageNumber: number, documentVersionId: string) => Promise<Blob>;
}

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

export const liveReviewPagesApi: ReviewPagesApi = {
  async loadPages(projectId, packageId) {
    const response = await fetch(`${BASE}/projects/${encodeURIComponent(projectId)}/packages/${encodeURIComponent(packageId)}/pages`, {
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) {
      let envelope: ErrorEnvelope;
      try {
        envelope = (await response.json()) as ErrorEnvelope;
      } catch {
        envelope = {
          error: 'unreadable_response',
          message: `The page list could not be loaded (HTTP ${response.status}).`,
          request_id: response.headers.get('x-request-id') ?? 'unknown',
        };
      }
      throw new ApiError(response.status, envelope);
    }
    return (await response.json()) as ReviewPagesOut;
  },
  loadPicture: (projectId, packageId, pageNumber, documentVersionId) => downloadPagePicture(projectId, packageId, pageNumber, documentVersionId),
};

/**
 * Page pictures shared by both panes (a combined sheet shows the same page on both sides), fetched
 * once and dropped as soon as no pane shows that page: a page that leaves the screen is released.
 */
export class PictureStore {
  private entries = new Map<string, { blob: Promise<Blob>; users: number }>();

  private fetchPicture: (pageNumber: number, documentVersionId: string) => Promise<Blob>;

  constructor(fetchPicture: (pageNumber: number, documentVersionId: string) => Promise<Blob>) {
    this.fetchPicture = fetchPicture;
  }

  static key(documentVersionId: string, pageNumber: number): string {
    return `${documentVersionId}|${pageNumber}`;
  }

  acquire(documentVersionId: string, pageNumber: number): Promise<Blob> {
    const key = PictureStore.key(documentVersionId, pageNumber);
    let entry = this.entries.get(key);
    if (!entry) {
      const blob = this.fetchPicture(pageNumber, documentVersionId);
      const made = { blob, users: 0 };
      // A failure is not remembered: Retry fetches afresh.
      blob.catch(() => {
        if (this.entries.get(key) === made) this.entries.delete(key);
      });
      entry = made;
      this.entries.set(key, entry);
    }
    entry.users += 1;
    return entry.blob;
  }

  release(documentVersionId: string, pageNumber: number) {
    const key = PictureStore.key(documentVersionId, pageNumber);
    const entry = this.entries.get(key);
    if (!entry) return;
    entry.users -= 1;
    if (entry.users <= 0) this.entries.delete(key);
  }

  /** How many pictures are held now (for tests). */
  get size(): number {
    return this.entries.size;
  }
}
