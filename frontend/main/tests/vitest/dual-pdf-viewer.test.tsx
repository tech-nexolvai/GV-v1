// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import type { ReviewItem } from '@/api/review-items';
import { DualPdfViewer, type ReviewPagesApi, type ReviewPagesOut } from '@/components/simple-review/dual-pdf-viewer';
import type { Decoded, Decoder } from '@/components/simple-review/dual-pdf-viewer-lib';

// Synthetic data only: nothing here comes from a client drawing.
const PIXEL = 'data:image/png;base64,iVBORw0KGgo=';

function page(n: number, docv: string, extra: Partial<ReviewPagesOut['documents'][number]['pages'][number]> = {}) {
  return {
    page_id: `${docv}-p${n}`,
    page_number: n,
    sheet_number: `X-${100 + n}`,
    picture_status: 'ready' as const,
    picture: { url: `/api/v1/pic/${docv}/${n}`, media_type: 'image/png', dpi: 300, width_px: 10800, height_px: 7200 },
    architect_regions: [],
    ...extra,
  };
}

function twoFiles(): ReviewPagesOut {
  return {
    package_revision_id: 'rev-1',
    revision_number: 2,
    combined_set: false,
    pages_without_pictures: 0,
    documents: [
      { document_id: 'doc-v', document_version_id: 'docv-vendor', kind: 'shop', side: 'vendor', file_name: 'sample-shop.pdf', page_count: 6, pages: [1, 2, 3, 4, 5, 6].map((n) => page(n, 'docv-vendor')) },
      { document_id: 'doc-a', document_version_id: 'docv-arch', kind: 'architectural', side: 'architect', file_name: 'sample-arch.pdf', page_count: 14, pages: Array.from({ length: 14 }, (_, i) => page(i + 1, 'docv-arch')) },
    ],
  };
}

function combined(): ReviewPagesOut {
  return {
    package_revision_id: 'rev-1',
    revision_number: 1,
    combined_set: true,
    pages_without_pictures: 0,
    documents: [
      {
        document_id: 'doc-v',
        document_version_id: 'docv-vendor',
        kind: 'shop',
        side: 'vendor',
        file_name: 'sample-combined.pdf',
        page_count: 3,
        pages: [
          page(1, 'docv-vendor'),
          page(2, 'docv-vendor', {
            architect_regions: [
              { view_id: 'view-a', tag: 'A1', role_confirmed: true, heading: null, polygon: [['0.6', '0.1'], ['0.9', '0.1'], ['0.9', '0.5'], ['0.6', '0.5']], bbox: { x0: '0.6', y0: '0.1', x1: '0.9', y1: '0.5' } },
            ],
          }),
          page(3, 'docv-vendor', {
            architect_regions: [
              { view_id: 'view-b', tag: 'B2', role_confirmed: false, heading: 'Sample plan view', polygon: [['0.1', '0.6'], ['0.4', '0.6'], ['0.4', '0.9'], ['0.1', '0.9']], bbox: { x0: '0.1', y0: '0.6', x1: '0.4', y1: '0.9' } },
            ],
          }),
        ],
      },
    ],
  };
}

const ITEM = {
  key: 'number:cand-101',
  what: 'Overall countertop width',
  page: 3,
  status: 'open',
  vendor_ref: { page_id: 'docv-vendor-p3', document_version_id: 'docv-vendor', page_number: 3, coordinate_space: 'stored', polygon: [['0.38', '0.30'], ['0.58', '0.30'], ['0.58', '0.35'], ['0.38', '0.35']] },
  architect_ref: { location: { page_id: 'docv-arch-p12', document_version_id: 'docv-arch', page_number: 12, coordinate_space: 'stored', polygon: [['0.30', '0.40'], ['0.52', '0.40'], ['0.52', '0.45'], ['0.30', '0.45']] }, reason: null },
} as unknown as ReviewItem;

function setup(data: () => ReviewPagesOut, { item = ITEM as ReviewItem | null, loadPicture }: { item?: ReviewItem | null; loadPicture?: ReviewPagesApi['loadPicture'] } = {}) {
  const decoded: { size: { w: number; h: number }; crop: unknown; closed: boolean }[] = [];
  const decoder: Decoder = async (_blob, crop, size) => {
    const entry = { size, crop, closed: false };
    decoded.push(entry);
    const out: Decoded = { source: PIXEL, width: size.w, height: size.h, close: () => { entry.closed = true; } };
    return out;
  };
  const api: ReviewPagesApi = {
    loadPages: vi.fn(async () => data()),
    loadPicture: loadPicture ?? vi.fn(async () => new Blob(['png'], { type: 'image/png' })),
  };
  const onClose = vi.fn();
  render(<DualPdfViewer projectId="proj-demo" packageId="pkg-demo" item={item} api={api} decoder={decoder} onClose={onClose} />);
  return { api, onClose, decoded };
}

const vendorPane = () => screen.getByRole('region', { name: /Vendor/ });
const architectPane = () => screen.getByRole('region', { name: /Architect/ });
const counter = (pane: HTMLElement) => within(pane).getByTestId('page-counter').textContent;

describe('DualPdfViewer (#1355)', () => {
  it('opens from an item at both spots, with the vendor spot ringed and the pictures drawn', async () => {
    const { decoded } = setup(twoFiles);
    const dialog = await screen.findByRole('dialog', { name: 'Both drawings' });
    expect(dialog).toBeTruthy();
    await waitFor(() => expect(counter(vendorPane())).toBe('3 / 6'));
    expect(counter(architectPane())).toBe('12 / 14');
    expect(await within(vendorPane()).findByRole('img', { name: /Vendor's drawing, page 3/ })).toBeTruthy();
    expect(within(vendorPane()).getByTestId('item-spot').getAttribute('aria-label')).toContain('Overall countertop width');
    expect(await within(architectPane()).findByRole('img', { name: /Architect's drawing, page 12/ })).toBeTruthy();
    // Decoded at the drawn size, never above the stored 10800 x 7200.
    await waitFor(() => expect(decoded.length).toBeGreaterThanOrEqual(2));
    for (const entry of decoded) {
      expect(entry.size.w).toBeLessThanOrEqual(10800);
      expect(entry.size.w).toBeLessThan(2000);
    }
  });

  it('synced pages: → moves both; unlinked, only the side last used; the page strip picks a page', async () => {
    setup(twoFiles);
    await waitFor(() => expect(counter(vendorPane())).toBe('3 / 6'));
    const user = userEvent.setup();
    await user.keyboard('{ArrowRight}');
    expect(counter(vendorPane())).toBe('4 / 6');
    expect(counter(architectPane())).toBe('13 / 14');
    await user.keyboard('{ArrowLeft}{ArrowLeft}');
    expect(counter(vendorPane())).toBe('2 / 6');
    expect(counter(architectPane())).toBe('11 / 14');

    const link = screen.getByRole('button', { name: /Pages linked/ });
    expect(link.getAttribute('aria-pressed')).toBe('true');
    await user.click(link);
    expect(screen.getByRole('button', { name: /Pages not linked/ }).getAttribute('aria-pressed')).toBe('false');
    await user.click(within(architectPane()).getByRole('button', { name: 'Next page' }));
    expect(counter(architectPane())).toBe('12 / 14');
    expect(counter(vendorPane())).toBe('2 / 6');
    await user.keyboard('{ArrowRight}');
    expect(counter(architectPane())).toBe('13 / 14');
    expect(counter(vendorPane())).toBe('2 / 6');

    await user.click(within(within(vendorPane()).getByRole('navigation', { name: 'Vendor pages' })).getByRole('button', { name: /^Page 5/ }));
    expect(counter(vendorPane())).toBe('5 / 6');
    expect(counter(architectPane())).toBe('13 / 14');
  });

  it('releases a page picture once it is off screen', async () => {
    const { decoded } = setup(twoFiles);
    await waitFor(() => expect(decoded.length).toBeGreaterThanOrEqual(2));
    const first = [...decoded];
    await userEvent.keyboard('{ArrowRight}');
    await waitFor(() => expect(first.every((entry) => entry.closed)).toBe(true));
  });

  it('a combined sheet: the architect side is the same page zoomed to its region; a suggestion says so', async () => {
    setup(combined, { item: null });
    await waitFor(() => expect(counter(vendorPane())).toBe('1 / 3'));
    expect(counter(architectPane())).toBe('1 / 3');
    expect(screen.queryByRole('button', { name: /Pages linked|Pages not linked/ })).toBeNull();
    expect(screen.getByText(/Same sheet/)).toBeTruthy();
    expect(architectPane().textContent).toContain("No architect's drawing is marked on page 1.");

    await userEvent.keyboard('{ArrowRight}');
    expect(counter(architectPane())).toBe('2 / 3');
    const confirmed = await within(architectPane()).findByTestId('architect-region');
    expect(confirmed.getAttribute('data-confirmed')).toBe('true');
    expect(within(architectPane()).queryByText(/Suggestion/)).toBeNull();

    await userEvent.keyboard('{ArrowRight}');
    const suggested = await within(architectPane()).findByTestId('architect-region');
    expect(suggested.getAttribute('data-confirmed')).toBe('false');
    expect(within(architectPane()).getByText(/Suggestion, not confirmed/)).toBeTruthy();
    expect(within(architectPane()).getByText(/Sample plan view/)).toBeTruthy();
  });

  it('a page whose picture is not made yet says so, and Check again reloads the list', async () => {
    const data = () => {
      const out = twoFiles();
      out.documents[0].pages[2] = page(3, 'docv-vendor', { picture_status: 'not_ready', picture: null });
      return out;
    };
    const { api } = setup(data);
    await waitFor(() => expect(counter(vendorPane())).toBe('3 / 6'));
    expect(await within(vendorPane()).findByText('Picture not made yet')).toBeTruthy();
    expect(api.loadPicture).not.toHaveBeenCalledWith('proj-demo', 'pkg-demo', 3, 'docv-vendor');
    await userEvent.click(within(vendorPane()).getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(api.loadPages).toHaveBeenCalledTimes(2));
  });

  it('a picture that fails to load shows an error with Retry, which fetches it again', async () => {
    let calls = 0;
    const loadPicture = vi.fn(async (_p: string, _k: string, pageNumber: number) => {
      if (pageNumber === 3 && calls++ === 0) throw new ApiError(500, { error: 'boom', message: 'The store did not answer.', request_id: 'req-1' });
      return new Blob(['png'], { type: 'image/png' });
    });
    setup(twoFiles, { loadPicture });
    await waitFor(() => expect(counter(vendorPane())).toBe('3 / 6'));
    const alert = await within(vendorPane()).findByRole('alert');
    expect(alert.textContent).toContain('The store did not answer.');
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(vendorPane()).findByRole('img', { name: /Vendor's drawing, page 3/ })).toBeTruthy();
  });

  it('the page list failing to load is an error with Retry', async () => {
    let first = true;
    const data = () => {
      if (first) {
        first = false;
        throw new ApiError(503, { error: 'down', message: 'The server is busy.', request_id: 'req-2' });
      }
      return twoFiles();
    };
    setup(data);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The server is busy.');
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(counter(vendorPane())).toBe('3 / 6'));
  });

  it('keyboard: focus stays inside, + and − zoom, W fits the width, L unlinks, Esc closes', async () => {
    const { onClose } = setup(twoFiles);
    const dialog = await screen.findByRole('dialog', { name: 'Both drawings' });
    await waitFor(() => expect(counter(vendorPane())).toBe('3 / 6'));
    const user = userEvent.setup();
    for (let i = 0; i < 30; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    const viewport = within(vendorPane()).getByRole('group', { name: /Vendor's drawing, page 3 of 6/ });
    viewport.focus();
    const zoom = () => within(vendorPane()).getByTestId('zoom-level').textContent;
    const fitted = zoom();
    await user.keyboard('+');
    expect(zoom()).not.toBe(fitted);
    await user.keyboard('w');
    expect(zoom()).toBe(fitted);
    await user.keyboard('l');
    expect(screen.getByRole('button', { name: /Pages not linked/ })).toBeTruthy();
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });
});
