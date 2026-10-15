import { afterEach, describe, expect, it, vi } from 'vitest';

import { downloadPagePicture } from '@/api/client';
import { liveReviewPagesApi, PictureStore } from '@/components/simple-review/dual-pdf-viewer-api';

// Synthetic ids only (#1362).
describe('PictureStore cancels unneeded downloads (#1362)', () => {
  it('gives each picture its own cancel signal and cancels it once no pane uses it', () => {
    const signals: AbortSignal[] = [];
    const store = new PictureStore((_page, _dvid, signal) => {
      signals.push(signal);
      return new Promise<Blob>(() => undefined);
    });
    void store.acquire('docv-1', 1);
    void store.acquire('docv-1', 2);
    expect(signals).toHaveLength(2);
    expect(signals[0]).not.toBe(signals[1]);
    store.release('docv-1', 1);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
    expect(store.size).toBe(1);
  });

  it('keeps a picture two panes share until both let it go', () => {
    const signals: AbortSignal[] = [];
    const store = new PictureStore((_page, _dvid, signal) => {
      signals.push(signal);
      return new Promise<Blob>(() => undefined);
    });
    void store.acquire('docv-1', 4);
    void store.acquire('docv-1', 4);
    expect(signals).toHaveLength(1);
    store.release('docv-1', 4);
    expect(signals[0].aborted).toBe(false);
    store.release('docv-1', 4);
    expect(signals[0].aborted).toBe(true);
    expect(store.size).toBe(0);
  });
});

describe('the viewer requests pass a cancel signal to fetch (#1362)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('downloadPagePicture and the page list hand the signal to fetch', async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith('/pages') ? new Response(JSON.stringify({ documents: [] }), { status: 200 }) : new Response(new Blob(['png']), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    await downloadPagePicture('proj-demo', 'pkg-demo', 2, 'docv-1', controller.signal);
    expect((fetchMock.mock.calls[0] as unknown[])[1]).toMatchObject({ signal: controller.signal });
    await liveReviewPagesApi.loadPicture('proj-demo', 'pkg-demo', 3, 'docv-1', controller.signal);
    expect((fetchMock.mock.calls[1] as unknown[])[1]).toMatchObject({ signal: controller.signal });
    await liveReviewPagesApi.loadPages('proj-demo', 'pkg-demo', controller.signal);
    expect((fetchMock.mock.calls[2] as unknown[])[1]).toMatchObject({ signal: controller.signal });
  });
});
