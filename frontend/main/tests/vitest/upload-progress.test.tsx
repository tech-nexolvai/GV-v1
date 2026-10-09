// @vitest-environment jsdom
import { File as NodeFile } from 'node:buffer';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createPackage, type UploadProgress } from '@/api/upload';
import { applyProgress, initialUploadView, percentOf } from '@/lib/upload-progress';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));
vi.mock('@/api/pdf', () => ({ countPdfPages: async () => 3 }));

/** A stand-in for the browser's XMLHttpRequest: records the request and reports progress in two ticks. */
class FakeUpload {
  static sent: FakeUpload[] = [];
  static status = 200;
  method = '';
  url = '';
  headers: Record<string, string> = {};
  body: unknown = null;
  status = 0;
  upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  open(method: string, url: string) { this.method = method; this.url = url; }
  setRequestHeader(name: string, value: string) { this.headers[name] = value; }
  send(body: unknown) {
    this.body = body;
    FakeUpload.sent.push(this);
    const total = (body as { size: number }).size;
    queueMicrotask(() => {
      this.upload.onprogress?.({ lengthComputable: true, loaded: Math.floor(total / 2), total });
      this.upload.onprogress?.({ lengthComputable: true, loaded: total, total });
      this.status = FakeUpload.status;
      this.onload?.();
    });
  }
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const posts: string[] = [];

beforeEach(() => {
  FakeUpload.sent = [];
  FakeUpload.status = 200;
  posts.length = 0;
  vi.stubGlobal('XMLHttpRequest', FakeUpload);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'POST') posts.push(url.replace(/^.*\/api\/v1/, ''));
    if (url.endsWith('/projects/p/packages')) return json({ id: 'pkg', current_revision_id: 'rev' }, 201);
    if (url.endsWith('/packages/pkg/documents')) {
      const kind = JSON.parse(String(init?.body)).kind as string;
      return json({ document_id: `doc-${kind}`, storage_key: `k-${kind}`, upload_url: `https://storage.example/${kind}?sig=1`, method: 'PUT', expires_at: '2026-10-09T12:00:00Z', required_headers: { 'Content-Type': 'application/pdf' } }, 201);
    }
    if (url.includes('/confirm')) return json({}, 201);
    if (url.endsWith('/extract')) return json({ accepted_id: 'x', package_revision_id: 'rev' }, 202);
    if (url.endsWith('/review-sessions')) return json({ id: 's', package_revision_id: 'rev', reviewer: 'r', created_at: '2026-10-09T10:00:00Z', completed_at: null }, 201);
    if (url.endsWith('/product-types')) return json([{ value: 'countertop', label: 'Countertop', published_checks: 3 }]);
    return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
  }));
});

function pdf(name: string, text: string) {
  return new NodeFile([text], name, { type: 'application/pdf' }) as unknown as File;
}

describe('upload progress arithmetic', () => {
  it('turns the reported steps into one bar per drawing and the three-step line', () => {
    let view = initialUploadView({ architectural: 100, shop: 200 });
    const feed = (progress: UploadProgress) => { view = applyProgress(view, progress); };
    feed({ step: 'Creating document set' });
    feed({ step: 'Hashing', file: 'a.pdf', kind: 'architectural' });
    expect(view.files.architectural.phase).toBe('preparing');
    feed({ step: 'Uploading', file: 'a.pdf', kind: 'architectural', loaded: 0, total: 100 });
    const words = view.step;
    feed({ step: 'Uploading', file: 'a.pdf', kind: 'architectural', loaded: 42, total: 100 });
    expect(percentOf(view.files.architectural)).toBe(42);
    expect(view.step).toBe(words); // percentages do not re-announce
    feed({ step: 'Confirming', file: 'a.pdf', kind: 'architectural' });
    expect(percentOf(view.files.architectural)).toBe(100);
    feed({ step: 'Uploaded', file: 'a.pdf', kind: 'architectural' });
    expect(view.files.architectural.phase).toBe('done');
    expect(view.files.shop.phase).toBe('waiting');
    expect(view.stage).toBe('upload');
    feed({ step: 'Queuing AI reading' });
    expect(view.stage).toBe('reading');
  });

  it('never shows more than 100% or a share of nothing', () => {
    expect(percentOf({ phase: 'sending', loaded: 150, total: 100 })).toBe(100);
    expect(percentOf({ phase: 'sending', loaded: 5, total: 0 })).toBe(0);
    expect(percentOf({ phase: 'preparing', loaded: 50, total: 100 })).toBe(0);
  });
});

describe('createPackage', () => {
  it('sends each file by XHR to the ticket URL with its method and headers, reporting real percentages', async () => {
    const seen: UploadProgress[] = [];
    const result = await createPackage('p', { vendor: 'Synthetic vendor', productType: 'countertop', architectural: pdf('arch.pdf', 'architect bytes'), shop: pdf('shop.pdf', 'shop drawing bytes!') }, (progress) => seen.push(progress));
    expect(result.packageId).toBe('pkg');
    expect(FakeUpload.sent.map((request) => [request.method, request.url, request.headers['Content-Type']])).toEqual([
      ['PUT', 'https://storage.example/architectural?sig=1', 'application/pdf'],
      ['PUT', 'https://storage.example/shop?sig=1', 'application/pdf'],
    ]);
    const shopBytes = seen.filter((progress) => progress.kind === 'shop' && progress.step === 'Uploading').map((progress) => progress.loaded);
    expect(shopBytes).toEqual([0, 9, 19]);
    expect(seen.filter((progress) => progress.step === 'Uploaded').map((progress) => progress.kind)).toEqual(['architectural', 'shop']);
    expect(posts).toContain('/projects/p/packages/pkg/extract');
  });

  it('refuses a write storage did not accept, keeping the saved set', async () => {
    FakeUpload.status = 403;
    await expect(createPackage('p', { vendor: 'Synthetic vendor', productType: 'countertop', architectural: pdf('arch.pdf', 'a'), shop: pdf('shop.pdf', 'b') }))
      .rejects.toMatchObject({ packageId: 'pkg' });
    expect(posts).not.toContain('/projects/p/packages/pkg/extract');
  });

  it('says the same file in both slots is sent once', async () => {
    const seen: UploadProgress[] = [];
    await createPackage('p', { vendor: 'Synthetic vendor', productType: 'countertop', architectural: pdf('set.pdf', 'same'), shop: pdf('set.pdf', 'same') }, (progress) => seen.push(progress));
    expect(FakeUpload.sent).toHaveLength(1);
    expect(seen.find((progress) => progress.kind === 'architectural')?.step).toBe('Same file as the shop drawings');
  });
});

describe('NewReviewForm while uploading', () => {
  it('shows the three steps and a bar per drawing, then opens the review', async () => {
    const { NewReviewForm } = await import('@/components/upload/NewReviewForm');
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const { container } = render(<NewReviewForm onCreated={onCreated} />);
    await user.type(screen.getByPlaceholderText('Who sent these drawings?'), 'Synthetic vendor');
    const [architectInput, shopInput] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="file"]'));
    fireEvent.change(architectInput, { target: { files: [new window.File(['architect bytes'], 'arch.pdf', { type: 'application/pdf' })] } });
    fireEvent.change(shopInput, { target: { files: [new window.File(['shop drawing bytes!'], 'shop.pdf', { type: 'application/pdf' })] } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start review' }).hasAttribute('disabled')).toBe(false));

    // Hold the shop drawing's write open so the panel can be read mid-upload.
    let release: () => void = () => {};
    const send = FakeUpload.prototype.send;
    vi.spyOn(FakeUpload.prototype, 'send').mockImplementation(function (this: FakeUpload, body: unknown) {
      if (this.url.includes('/shop')) {
        FakeUpload.sent.push(this);
        this.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 20 });
        release = () => { this.status = 200; this.onload?.(); };
        return;
      }
      send.call(this, body);
    });
    await user.click(screen.getByRole('button', { name: 'Start review' }));

    const steps = await screen.findByRole('list', { name: 'Review set-up' });
    expect(within(steps).getByText(/Upload/).closest('li')?.getAttribute('aria-current')).toBe('step');
    await waitFor(() => expect(screen.getByRole('progressbar', { name: /Shop drawings: Sending 25%/ }).getAttribute('aria-valuenow')).toBe('25'));
    expect(screen.getByRole('progressbar', { name: /Architect's drawings: Uploaded/ }).getAttribute('aria-valuenow')).toBe('100');
    expect(screen.getByText(/AI reading then takes a few minutes/)).toBeTruthy();

    release();
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('pkg'));
  });
});
