import { useEffect, useState } from 'react';

import { ApiError, downloadEvidenceCrop, downloadPagePicture } from '@/api/client';

/**
 * Page pictures and evidence crops for one opening of the drawing viewer (#1045).
 *
 * Each picture is downloaded once and shared by the page strip and the main view; every object URL
 * is revoked when the viewer closes. A page picture that is not rendered yet is reported as
 * "not ready" — the viewer never asks the worker to render it, and never reads anything.
 */
export class BlobCache {
  private entries = new Map<string, Promise<string>>();
  private urls = new Set<string>();

  load(key: string): Promise<string> {
    let entry = this.entries.get(key);
    if (!entry) {
      entry = fetchBlob(key).then((blob) => {
        const url = URL.createObjectURL(blob);
        this.urls.add(url);
        return url;
      });
      // A failure is not remembered: "try again" fetches afresh.
      entry.catch(() => this.entries.delete(key));
      this.entries.set(key, entry);
    }
    return entry;
  }

  forget(key: string) {
    this.entries.delete(key);
  }

  dispose() {
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls.clear();
    this.entries.clear();
  }
}

export function useBlobCache(): BlobCache {
  const [cache] = useState(() => new BlobCache());
  useEffect(() => () => cache.dispose(), [cache]);
  return cache;
}

export type BlobState =
  | { status: 'loading' }
  | { status: 'ready'; url: string }
  | { status: 'not-ready' }
  | { status: 'error'; error: string };

export function pageKey(projectId: string, packageId: string, page: number, documentVersionId: string): string {
  return ['page', projectId, packageId, documentVersionId, String(page)].join('|');
}

export function cropKey(projectId: string, packageId: string, observationId: string): string {
  return ['crop', projectId, packageId, observationId].join('|');
}

/** The key says everything needed to fetch, so the effect depends on one string. */
async function fetchBlob(key: string): Promise<Blob> {
  const [kind, projectId, packageId, a, b] = key.split('|');
  if (kind === 'page') return downloadPagePicture(projectId, packageId, Number(b), a);
  // The crop endpoint verifies the stored digest; anything that is not an image is not shown.
  const blob = await downloadEvidenceCrop(projectId, packageId, a);
  if (!blob.type.startsWith('image/')) throw new Error('The stored evidence is not an image.');
  return blob;
}

/** One picture or crop: loading, ready, not rendered yet (404), or an error — and a way to retry. */
export function useBlob(cache: BlobCache, key: string | null): [BlobState, () => void] {
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState<{ key: string; state: BlobState } | null>(null);

  useEffect(() => {
    if (key === null) return;
    let live = true;
    cache.load(key).then(
      (url) => {
        if (live) setLoaded((prev) => (prev?.key === key && prev.state.status === 'ready' && prev.state.url === url ? prev : { key, state: { status: 'ready', url } }));
      },
      (error: unknown) => {
        if (!live) return;
        // The picture API says "not ready" only for a page whose picture the worker has not made yet;
        // a page that does not exist is a plain 404 and must not read as "try again later".
        const state: BlobState = error instanceof ApiError && error.status === 404 && /not ready/i.test(error.message)
          ? { status: 'not-ready' }
          : error instanceof ApiError && error.status === 404
            ? { status: 'error', error: 'This page is not in the drawing set.' }
            : { status: 'error', error: error instanceof Error ? error.message : String(error) };
        setLoaded({ key, state });
      },
    );
    return () => {
      live = false;
    };
  }, [cache, key, attempt]);

  const retry = () => {
    if (key === null) return;
    cache.forget(key);
    setLoaded(null);
    setAttempt((n) => n + 1);
  };
  // Derived, not reset in the effect: another key's answer is simply not this key's.
  const state: BlobState = loaded && loaded.key === key ? loaded.state : { status: 'loading' };
  return [state, retry];
}
