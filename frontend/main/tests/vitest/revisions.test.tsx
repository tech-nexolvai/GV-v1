// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Revision, RevisionComparison } from '@/api/client';
import { RevisionSwitcher } from '@/components/revisions/revision-switcher';
import { ReadOnlyBanner, EarlierRevisionView, SignedFiles } from '@/components/revisions/earlier-revision-view';
import { WallGlyph } from '@/components/results/wall-glyph';
import { SignOffDialog } from '@/components/review/signoff-panel';
import { listRevisions, setScreenRevision, startRevision } from '@/api/client';
import { WhatChangedView } from '@/components/revisions/what-changed-panel';
import { NewRevisionDialog } from '@/components/revisions/new-revision-dialog';
import {
  carriedWallsDrivingResults,
  carriedWords,
  changedCountertops,
  changedPages,
  pageSummary,
  productWord,
  revisionStatus,
  revisionTitle,
} from '@/lib/revisions';

// Synthetic data only (#1180): no client drawing, vendor or value.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  getCountertopResults: vi.fn(async () => ({
    package_id: 'pkg',
    revision_id: 'rev-1',
    items: [
      {
        finding_id: 'f1', row_id: 'row-1', page_number: 3, label: 'Synthetic countertop row', row_location: null,
        outcome: 'FAIL', needs_decision: false, printed_overall: { numerator: '88', denominator: '1', display: '88"' },
        pieces: [], field_cut_per_end: null, field_cut_count: null,
        expected_total: { numerator: '90', denominator: '1', display: '90"' }, delta: null,
        hold: null, reviewer_decision: { action: 'confirm', note: null, actor: 'Synthetic Reviewer', time: '2026-10-01T10:00:00Z', carried_over: false, carried_from_finding_id: null },
        wall_layout: { config: null, label: null, source: 'not established' },
        agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
      },
    ],
  })),
}));

function revision(number: number, overrides: Partial<Revision> = {}): Revision {
  return {
    revision_id: `rev-${number}`,
    revision_number: number,
    state: number === 2 ? 'UPLOADING' : 'SUPERSEDED',
    created_at: '2026-10-01T10:00:00Z',
    current: number === 2,
    read_only: number !== 2,
    supersedes_revision_number: number === 2 ? 1 : null,
    superseded_by_revision_number: number === 1 ? 2 : null,
    signed_off_at: null,
    signed_off_by: null,
    drawings: [],
    ...overrides,
  } as Revision;
}

const comparison = {
  package_id: 'pkg',
  before_revision_number: 1,
  after_revision_number: 2,
  pages_added: 1,
  pages_removed: 0,
  pages_changed: 1,
  pages_unchanged: 3,
  countertops_read: true,
  countertops_checked: true,
  drawings: [
    {
      kind: 'shop', document_id: 'd-shop', file_name: 'synthetic.pdf', same_file: false, compared: true, reason: null,
      pages: [
        { kind: 'shop', document_id: 'd-shop', file_name: 'synthetic.pdf', page_number: 1, change: 'unchanged' },
        { kind: 'shop', document_id: 'd-shop', file_name: 'synthetic.pdf', page_number: 2, change: 'changed' },
        { kind: 'shop', document_id: 'd-shop', file_name: 'synthetic.pdf', page_number: 3, change: 'added' },
      ],
    },
    { kind: 'architectural', document_id: 'd-arch', file_name: null, same_file: true, compared: true, reason: null, pages: [
      { kind: 'architectural', document_id: 'd-arch', file_name: null, page_number: 1, change: 'unchanged' },
      { kind: 'architectural', document_id: 'd-arch', file_name: null, page_number: 2, change: 'unchanged' },
    ] },
  ],
  countertops: [
    { page_number: 1, label: 'Countertop row on page 1', change: 'same',
      before: { outcome: 'PASS', needs_decision: false, printed_overall: null, expected_total: null, delta: null },
      after: { outcome: 'PASS', needs_decision: false, printed_overall: null, expected_total: null, delta: null } },
    { page_number: 2, label: 'Countertop row on page 2', change: 'changed',
      before: { outcome: 'FAIL', needs_decision: false, printed_overall: null, expected_total: null, delta: null },
      after: { outcome: 'PASS', needs_decision: false, printed_overall: null, expected_total: null, delta: null } },
  ],
} as unknown as RevisionComparison;

describe('revision words (#1180)', () => {
  it('names a revision and where it stands', () => {
    expect(revisionTitle(2, 2)).toBe('Revision 2 of 2');
    expect(revisionStatus({ current: true, signed_off_at: null })).toBe('Current');
    expect(revisionStatus({ current: false, signed_off_at: '2026-10-01T10:00:00Z' })).toBe('Read-only · signed off');
    expect(productWord('countertop')).toBe('Countertop');
  });

  it('counts and lists only what changed', () => {
    expect(pageSummary(comparison)).toBe('1 added · 1 changed · 0 removed · 3 unchanged');
    expect(changedPages(comparison).map((page) => [page.pageNumber, page.change])).toEqual([[2, 'changed'], [3, 'added']]);
    expect(changedCountertops(comparison).map((row) => row.page_number)).toEqual([2]);
  });
});

describe('revision switcher', () => {
  it('says "Revision 2 of 2" and switches to an earlier, read-only revision', async () => {
    const user = userEvent.setup();
    const onView = vi.fn();
    render(<RevisionSwitcher revisions={[revision(1), revision(2)]} viewing={2} onView={onView} />);

    const trigger = screen.getByRole('button', { name: /Revision 2 of 2/ });
    expect(trigger.textContent).toContain('Revision 2 of 2');
    await user.click(trigger);
    expect(await screen.findByText('Read-only')).toBeTruthy();
    await user.click(screen.getByRole('menuitemradio', { name: /Revision 1/ }));
    expect(onView).toHaveBeenCalledWith(1);
  });

  it('is not shown while a review has one revision', () => {
    const { container } = render(<RevisionSwitcher revisions={[revision(1, { current: true, read_only: false })]} viewing={1} onView={() => {}} />);
    expect(container.innerHTML).toBe('');
  });
});

describe('an earlier revision is read-only', () => {
  it('shows the banner and goes back to the newest', async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    render(<ReadOnlyBanner revision={revision(1, { signed_off_by: 'Synthetic Signer' })} newest={2} onBack={onBack} />);

    expect(screen.getByRole('status').textContent).toContain('Revision 1 is read-only.');
    expect(screen.getByRole('status').textContent).toContain('signed off by Synthetic Signer');
    await user.click(screen.getByRole('button', { name: 'Back to revision 2' }));
    expect(onBack).toHaveBeenCalled();
  });

  it('lists its results as recorded, with nothing to decide', async () => {
    render(<EarlierRevisionView projectId="p" packageId="pkg" revision={revision(1)} newest={2} onBack={() => {}} />);

    expect(await screen.findByText('Synthetic countertop row')).toBeTruthy();
    expect(screen.getByText('Synthetic Reviewer')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /decide/i })).toBeNull();
  });
});

describe('what changed', () => {
  it('lists changed pages and the countertops whose result changed', () => {
    render(<WhatChangedView comparison={comparison} />);

    expect(screen.getByText(/What changed since revision/).textContent).toContain('What changed since revision 1');
    expect(screen.getByText('1 added · 1 changed · 0 removed · 3 unchanged')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Pages that changed' }).textContent).toContain('ChangedShop drawings, page 2');
    const countertops = screen.getByRole('region', { name: 'Countertops that changed' });
    expect(countertops.textContent).toContain('Countertop row on page 2');
    expect(countertops.textContent).not.toContain('Countertop row on page 1');
  });

  it('says when the new revision has not been checked yet', () => {
    render(<WhatChangedView comparison={{ ...comparison, countertops_checked: false, countertops: [] }} />);
    expect(screen.getByText(/checks have not run on this revision yet/)).toBeTruthy();
  });

  it('says the new revision has not been read yet, instead of every countertop "gone"', () => {
    render(<WhatChangedView comparison={{ ...comparison, countertops_read: false, countertops_checked: false, countertops: [] }} />);
    expect(screen.getByText(/has not been read yet/).textContent).toContain('Revision 2 has not been read yet');
    expect(screen.queryByText(/checks have not run/)).toBeNull();
  });

  it('names a page that moved and where it was', () => {
    const moved = {
      ...comparison,
      drawings: [{ ...comparison.drawings[0], pages: [{ kind: 'shop', document_id: 'd-shop', file_name: null, page_number: 3, before_page_number: 2, change: 'moved' }] }],
    } as unknown as RevisionComparison;
    render(<WhatChangedView comparison={moved} />);
    expect(screen.getByRole('region', { name: 'Pages that changed' }).textContent).toContain('MovedShop drawings, page 3 (was page 2)');
  });
});

describe('new revision', () => {
  it('needs only the shop drawing, then uploads and reports the new revision', async () => {
    const user = userEvent.setup();
    const upload = vi.fn(async () => 2);
    const onStarted = vi.fn();
    render(
      <NewRevisionDialog
        open
        onOpenChange={() => {}}
        projectId="p"
        packageId="pkg"
        vendor="Synthetic Vendor"
        product="Countertop"
        currentRevision={1}
        onStarted={onStarted}
        upload={upload}
      />,
    );
    expect(screen.getByText(/A change to only the architect's drawings is not a new revision yet/)).toBeTruthy();

    expect(screen.getByText('Synthetic Vendor')).toBeTruthy();
    expect(screen.getByText(/use the architect's drawings of revision 1 again/)).toBeTruthy();
    const start = screen.getByRole('button', { name: 'Start revision 2' });
    expect((start as HTMLButtonElement).disabled).toBe(true);

    const file = new File(['%PDF-1.7 synthetic'], 'synthetic-reissue.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText('Shop drawings (required)'), file);
    expect((start as HTMLButtonElement).disabled).toBe(false);
    await user.click(start);

    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(2));
    expect(upload).toHaveBeenCalledWith('p', 'pkg', { expectedRevision: 1, shop: file, architectural: null }, expect.any(Function));
  });

  it('finishes an unfinished revision instead of starting another', () => {
    render(
      <NewRevisionDialog open onOpenChange={() => {}} projectId="p" packageId="pkg" vendor="Synthetic Vendor" product="Countertop" currentRevision={2} unfinished onStarted={() => {}} upload={vi.fn(async () => 2)} />,
    );
    expect(screen.getByRole('dialog', { name: 'Finish revision 2' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Finish revision 2' })).toBeTruthy();
    expect(screen.getByText(/use the architect's drawings of revision 1 again/)).toBeTruthy();
  });
});

describe('carried answers are visible (#1180)', () => {
  it('a carried wall answer names its revision and person, on the glyph and in the words', () => {
    expect(carriedWords(1, 'Synthetic Reviewer')).toBe('From revision 1 · Synthetic Reviewer');
    expect(carriedWords(null, 'Synthetic Reviewer')).toBeNull();
    render(<WallGlyph layout={{ config: 'back_only', label: 'back wall only', source: 'reviewer', carried_from_revision: 1, carried_by: 'Synthetic Reviewer', carried_given_at: null }} />);
    expect(document.querySelector('[data-slot="wall-carried"]')?.textContent).toBe('From revision 1 · Synthetic Reviewer');
  });

  it('sign-off asks once to confirm carried answers that results rest on', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const carried = carriedWallsDrivingResults([
      { row_id: 'a', label: 'Synthetic row A', page_number: 1, outcome: 'PASS', wall_layout: { source: 'reviewer', carried_from_revision: 1, carried_by: 'Synthetic Reviewer' } },
      { row_id: 'b', label: 'Synthetic row B', page_number: 2, outcome: null, wall_layout: { source: 'reviewer', carried_from_revision: 1, carried_by: 'Synthetic Reviewer' } },
      { row_id: 'c', label: 'Synthetic row C', page_number: 3, outcome: 'FAIL', wall_layout: { source: 'reviewer', carried_from_revision: null, carried_by: null } },
    ]);
    expect(carried.map((item) => item.rowId)).toEqual(['a']);
    render(
      <SignOffDialog open onOpenChange={() => {}} signer="Synthetic Reviewer" vendor="Synthetic Vendor" revision={2} scope={{ total: 1, countertops: null, countertopsFailed: false, otherChecks: null }} ready busy={false} error={null} onConfirm={onConfirm} carried={carried} />,
    );
    const sign = screen.getByRole('button', { name: 'Sign off' }) as HTMLButtonElement;
    expect(screen.getByRole('region', { name: 'Answers carried from an earlier revision' }).textContent).toContain('Synthetic row A: wall answer from revision 1 · Synthetic Reviewer');
    expect(sign.disabled).toBe(true);
    await user.click(screen.getByLabelText('These answers still apply to this revision'));
    expect(sign.disabled).toBe(false);
    await user.click(sign);
    expect(onConfirm).toHaveBeenCalledWith(true);
  });
});

describe('a screen names the revision it shows on every write', () => {
  it('sends X-GV-Revision on writes to that package only', async () => {
    const seen: Array<[string, string | null]> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      seen.push([String(input), headers.get('X-GV-Revision')]);
      return new Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));
    setScreenRevision('pkg', 1);
    await startRevision('p', 'pkg', { expected_revision: 1, shop: { sha256: 'a'.repeat(64) } }).catch(() => undefined);
    await listRevisions('p', 'pkg');
    await startRevision('p', 'other', { expected_revision: 1, shop: { sha256: 'a'.repeat(64) } }).catch(() => undefined);
    setScreenRevision('pkg', null);
    vi.unstubAllGlobals();

    expect(seen.map(([, header]) => header)).toEqual(['1', null, null]);
  });
});

describe('an earlier revision keeps its signed files', () => {
  it('offers the signed files only when that revision was signed off', () => {
    const { rerender } = render(<SignedFiles projectId="p" packageId="pkg" revision={revision(1)} />);
    expect(screen.queryByRole('button', { name: /Signed PDF/ })).toBeNull();
    rerender(<SignedFiles projectId="p" packageId="pkg" revision={revision(1, { signed_off_at: '2026-10-01T10:00:00Z', signed_off_by: 'Synthetic Signer' })} />);
    expect(screen.getByRole('region', { name: 'Signed files of revision 1' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Signed PDF/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Workbook/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Redline/ })).toBeTruthy();
  });
});
