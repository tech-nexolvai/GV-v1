// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AiReviewOut, ReviewItemsOut } from '@/api/review-items';
import type { FinalCloudApi } from '@/api/final-cloud';
import { ItemDetail } from '@/components/simple-review/item-detail';
import { AiReview } from '@/components/simple-review/ai-review';
import { PicturesProvider } from '@/components/simple-review/item-pictures';
import { tidyWhy } from '@/lib/review-items';
import { allSettled, reviewItems } from './fixtures/review-items';

// Set 3 QA (2026-10-16), frontend and wording. Synthetic data only: every value is invented.
vi.mock('@/api/config', () => ({ projectId: () => 'proj-demo' }));

const { SimpleReviewPage } = await import('@/pages/SimpleReviewPage');

const API = '*/api/v1/projects/proj-demo/packages/pkg-demo';
let current: ReviewItemsOut;
let loads = 0;

const server = setupServer(
  http.get(`${API}/review-items`, () => {
    loads += 1;
    return HttpResponse.json(current);
  }),
  http.get(API, () => HttpResponse.json({ id: 'pkg-demo', vendor: 'Sample Stoneworks', current_revision_number: 2 })),
);

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  current = reviewItems();
  loads = 0;
});

const pictures = async () => 'data:image/png;base64,iVBORw0KGgo=';

function finalApi(): FinalCloudApi {
  return {
    facts: vi.fn(async () => ({ signer: 'Sample Reviewer', carried: [] })),
    signOff: vi.fn(async () => {}),
    prepare: vi.fn(async () => 'preparing' as const),
    status: vi.fn(async () => 'preparing' as const),
    download: vi.fn(async () => new Blob(['%PDF'])),
  };
}

/** The same list once the worker finished the re-check: nothing is re-checking any more. */
function recheckDone(): ReviewItemsOut {
  return reviewItems((draft) => {
    for (const entry of draft.items) if (entry.status === 'rechecking') entry.status = 'open';
    draft.header = { ...draft.header, rechecking: 0 };
  });
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe('the review screen follows a re-check by itself (QA 1)', () => {
  it('asks again while an item is re-checking, and stops once nothing is', async () => {
    render(<SimpleReviewPage packageId="pkg-demo" pictures={pictures} assistantAvailable={false} onOpenAdvanced={vi.fn()} pollMs={30} />);
    expect(await screen.findByText(/Re-checking 1 item with your answers/)).toBeTruthy();
    current = recheckDone();
    await waitFor(() => expect(screen.queryByText(/Re-checking 1 item/)).toBeNull());
    const settled = loads;
    await sleep(150);
    expect(loads).toBe(settled);
  });

  it('does not ask again when nothing is re-checking or running', async () => {
    current = recheckDone();
    render(<SimpleReviewPage packageId="pkg-demo" pictures={pictures} assistantAvailable={false} onOpenAdvanced={vi.fn()} pollMs={30} />);
    await screen.findByTestId('ai-read-line');
    await sleep(150);
    expect(loads).toBe(1);
  });

  it('tells the sidebar the package changed after sign-off', async () => {
    current = allSettled();
    const finalCloud = finalApi();
    const onPackageChanged = vi.fn();
    render(<SimpleReviewPage packageId="pkg-demo" pictures={pictures} assistantAvailable={false} onOpenAdvanced={vi.fn()} finalCloud={finalCloud} onPackageChanged={onPackageChanged} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Create final cloud PDF' }));
    const dialog = await screen.findByRole('dialog', { name: 'Sign off and create the final cloud PDF?' });
    await within(dialog).findByText('Sample Reviewer');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign off and create' }));
    await waitFor(() => expect(onPackageChanged).toHaveBeenCalled());
  });
});

function renderDetail(change: (draft: ReviewItemsOut) => void, key: string, extra: Partial<React.ComponentProps<typeof ItemDetail>> = {}) {
  const data = reviewItems(change);
  const item = data.items.find((entry) => entry.key === key)!;
  const onAct = vi.fn();
  render(
    <PicturesProvider load={pictures}>
      <ItemDetail item={item} busy={null} error={null} onAct={onAct} onOpenPdfs={vi.fn()} {...extra} />
    </PicturesProvider>,
  );
  return { onAct };
}

describe('a number item says what to check, without contradicting itself (QA 3)', () => {
  const held = (draft: ReviewItemsOut) => {
    const number = draft.items.find((entry) => entry.key === 'number:cand-102')!;
    number.why = 'A reader could not read it clearly.';
  };

  it('shows "What to check" with the value, the place and the stored reason', () => {
    renderDetail(held, 'number:cand-102');
    const block = screen.getByTestId('what-to-check');
    expect(within(block).getByRole('heading', { name: 'What to check' })).toBeTruthy();
    expect(block.textContent).toContain('30"');
    expect(block.textContent).toContain('A reader could not read it clearly.');
  });

  it('never says "Both AIs read this" beside a reason that a reader could not read it', () => {
    renderDetail(held, 'number:cand-102');
    const detail = document.querySelector('[data-slot="item-detail"]')!;
    expect(detail.textContent).not.toContain('Both AIs read this');
    // The reason is said once, inside the block.
    expect(detail.textContent!.split('A reader could not read it clearly.').length - 1).toBe(1);
  });
});

describe('the Fix box never previews a value it will refuse (QA 4)', () => {
  it('a mm value shows the inches-only hint at once, never "Recorded as", and cannot be saved', async () => {
    renderDetail(() => {}, 'number:cand-101', { fixOpen: true });
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    await userEvent.type(within(dialog).getByRole('textbox', { name: 'What the drawing shows' }), '25 mm');
    const hint = within(dialog).getByRole('status');
    expect(hint.textContent).not.toContain('Recorded as');
    expect(hint.textContent).toContain('Inches only, e.g. 1"');
    expect((within(dialog).getByRole('button', { name: 'Save the correction' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('wording (QA 5)', () => {
  it('adds the missing full stop before "The vendor wrote"', () => {
    expect(tidyWhy('Choose the wall layout: a field cut is added at each end The vendor wrote 96": right with two walls.')).toBe(
      'Choose the wall layout: a field cut is added at each end. The vendor wrote 96": right with two walls.',
    );
    expect(tidyWhy('Already a sentence. The vendor wrote 96".')).toBe('Already a sentence. The vendor wrote 96".');
  });

  it('the wall reason on screen has its full stop', () => {
    renderDetail((draft) => {
      draft.items.find((entry) => entry.key === 'wall:row-7')!.why = 'A field cut is added at each end The vendor wrote 96": right with two walls.';
    }, 'wall:row-7');
    expect(document.querySelector('[data-slot="item-detail"]')!.textContent).toContain('at each end. The vendor wrote');
  });

  it('the architect note shows its first reason; the others wait behind Why', () => {
    renderDetail((draft) => {
      draft.items.find((entry) => entry.key === 'number:cand-102')!.architect_ref = { location: null, reason: 'No architect view was matched; the AIs picked different views; code found no fit' };
    }, 'number:cand-102');
    const note = screen.getByTestId('architect-note');
    expect(note.textContent).not.toContain(';');
    expect(within(note).getByText('No architect view was matched.')).toBeTruthy();
    const why = within(note).getByText('Why');
    expect(why.closest('details')?.textContent).toContain('The AIs picked different views.');
    expect(why.closest('details')?.textContent).toContain('Code found no fit.');
  });
});

describe('a page the AI page review did not run on says why (QA 6)', () => {
  const review = (reason: string | null): AiReviewOut =>
    ({
      counts: { compared: 0, same: 0, different: 0, unsure: 0, unreadable: 0, not_shown: 0, needs_person: 0 },
      pages: [
        { page: 12, status: 'done', cost_usd: null, rows: [] },
        { page: 13, status: 'not_run', cost_usd: null, reason, rows: [] },
      ],
    }) as unknown as AiReviewOut;

  it('with a stored reason', () => {
    render(<AiReview review={review('No vendor drawing was found on this sheet.')} onLook={vi.fn()} />);
    expect(screen.getByTestId('ai-review-pages').textContent).toContain('Page 13 was not read by the AI: no vendor drawing was found on this sheet.');
  });

  it('without one, as before', () => {
    render(<AiReview review={review(null)} onLook={vi.fn()} />);
    expect(screen.getByTestId('ai-review-pages').textContent).toContain('Page 13 was not read by the AI.');
  });
});
