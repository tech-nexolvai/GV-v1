// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ReviewHeader } from '@/components/simple-review/review-header';
import { FailuresStrip } from '@/components/simple-review/failures-strip';
import { ItemRow } from '@/components/simple-review/item-row';
import { ItemDetail } from '@/components/simple-review/item-detail';
import { NotCheckedGroup } from '@/components/simple-review/not-checked-group';
import { FinalBar } from '@/components/simple-review/final-bar';
import { LoadError, NothingNeedsYou, ReviewSkeleton, UnaccountedBanner } from '@/components/simple-review/states';
import { PicturesProvider } from '@/components/simple-review/item-pictures';
import { summarize } from '@/lib/review-items';
import { ApiError } from '@/api/client';
import { allSettled, reviewItems } from './fixtures/review-items';

// Synthetic data only: nothing here comes from a client drawing.
const data = reviewItems();
const item = (key: string) => data.items.find((entry) => entry.key === key)!;
const pictures = vi.fn(async () => 'data:image/png;base64,iVBORw0KGgo=');

describe('ReviewHeader', () => {
  it('names the set and says what the AI read, numbers in the number face', () => {
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={() => {}} draft={{ status: 'idle' }} />);
    expect(screen.getByRole('heading', { level: 2, name: 'Sample Stoneworks' })).toBeTruthy();
    const line = screen.getByTestId('ai-read-line');
    expect(line.textContent).toBe('AI read 12 pages · 148 numbers · 131 confirmed by AI');
    expect(within(line).getByText('148').className).toContain('num');
    expect(screen.getByRole('button', { name: /Open PDFs/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Draft cloud PDF/ })).toBeTruthy();
  });

  it('a draft that cannot be made yet is named, not hidden', () => {
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={null} draft={{ status: 'idle' }} />);
    const button = screen.getByRole('button', { name: /Draft cloud PDF/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toMatch(/not available/i);
  });
});

describe('FailuresStrip', () => {
  it('lists each result to be clouded as wrong with a glyph, the word and one Agree', async () => {
    const onAgree = vi.fn();
    render(<FailuresStrip failures={data.failures} busyKey={null} errors={{}} onAgree={onAgree} />);
    const strip = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    expect(within(strip).getAllByText('Wrong')).toHaveLength(2);
    expect(strip.querySelectorAll('[data-slot="failure-row"] [data-outcome-icon="FAIL"]')).toHaveLength(2);
    await userEvent.click(within(strip).getAllByRole('button', { name: /Agree/ })[0]);
    expect(onAgree).toHaveBeenCalledWith(data.failures[0]);
  });

  it('an agreed result says so and offers no button', () => {
    const agreed = reviewItems((draft) => { draft.failures[0].status = 'done'; draft.failures[0].actions = []; });
    render(<FailuresStrip failures={agreed.failures} busyKey={null} errors={{}} onAgree={() => {}} />);
    expect(screen.getAllByRole('button', { name: /Agree/ })).toHaveLength(1);
    expect(screen.getByText('Agreed')).toBeTruthy();
  });

  it('shows nothing when no result fails', () => {
    const { container } = render(<FailuresStrip failures={[]} busyKey={null} errors={{}} onAgree={() => {}} />);
    expect(container.textContent).toBe('');
  });
});

describe('ItemRow', () => {
  it('is one closed line: what, place, page and the guess in the number face', () => {
    const onToggle = vi.fn();
    render(<ItemRow item={item('number:cand-101')} open={false} active={false} onToggle={onToggle} />);
    const button = screen.getByRole('button', { name: /Overall countertop width/ });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(button.textContent).toContain('Countertop 1');
    expect(screen.getByText('p.3').className).toContain('num');
    expect(screen.getByText('96 1/4"').className).toContain('num');
    expect(screen.getByText('Needs you')).toBeTruthy();
    expect(button.querySelector('[data-outcome-icon="REVIEW_REQUIRED"]')).toBeTruthy();
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalled();
  });

  it('without a guess it says to check by hand', () => {
    render(<ItemRow item={item('countertop:find-5')} open={false} active={false} onToggle={() => {}} />);
    expect(screen.getByText('Check by hand')).toBeTruthy();
  });

  it('says done and re-checking with a glyph and a word', () => {
    const { rerender } = render(<ItemRow item={item('number:cand-090')} open={false} active={false} onToggle={() => {}} />);
    expect(screen.getByText('Checked by you')).toBeTruthy();
    rerender(<ItemRow item={item('number:cand-120')} open={false} active={false} onToggle={() => {}} />);
    expect(screen.getByText('Re-checking')).toBeTruthy();
  });
});

describe('ItemDetail', () => {
  function renderDetail(key: string, onAct = vi.fn(), onOpenPdfs = vi.fn()) {
    render(
      <PicturesProvider load={pictures}>
        <ItemDetail item={item(key)} busy={null} error={null} onAct={onAct} onOpenPdfs={onOpenPdfs} />
      </PicturesProvider>,
    );
    return { onAct, onOpenPdfs };
  }

  it('shows the vendor page with the spot ringed, the architect page and the close-up', async () => {
    renderDetail('number:cand-101');
    expect(await screen.findByRole('img', { name: /Vendor page 3, the number ringed/ })).toBeTruthy();
    expect(screen.getByRole('img', { name: /Architect page 12/ })).toBeTruthy();
    expect(await screen.findByRole('img', { name: /Close-up/ })).toBeTruthy();
    // Each page is drawn once its own size is known; the ring is drawn in the page's coordinates.
    await waitFor(() => expect(document.querySelectorAll('img.hidden')).toHaveLength(2));
    document.querySelectorAll('img.hidden').forEach((img) => fireEvent.load(img));
    await waitFor(() => expect(document.querySelectorAll('[data-slot="spot-ring"]')).toHaveLength(2));
  });

  it("says why there is no architect page instead of an empty box", () => {
    renderDetail('number:cand-102');
    expect(screen.getByText('The architect does not write cabinet widths on this sheet.')).toBeTruthy();
  });

  it("shows the AI's best guess large, where it came from, and each AI's reading", () => {
    renderDetail('number:cand-101');
    const guess = screen.getByTestId('best-guess');
    expect(guess.textContent).toBe('96 1/4"');
    expect(guess.className).toContain('num');
    expect(screen.getByText("Picked from the AIs' readings")).toBeTruthy();
    expect(screen.getByText('AI 2').parentElement?.textContent).toContain('96 3/4');
  });

  it('Confirm sends one click; Fix offers the other reading to pick, never a box to type in', async () => {
    const { onAct } = renderDetail('number:cand-101');
    await userEvent.click(screen.getByRole('button', { name: /Confirm/ }));
    expect(onAct).toHaveBeenCalledWith('confirm');
    await userEvent.click(screen.getByRole('button', { name: /Fix/ }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /96 3\/4"/ }));
    expect(onAct).toHaveBeenCalledWith('fix', '96 3/4"');
    expect(document.querySelector('input, textarea')).toBeNull();
  });

  it('Open PDFs opens both drawings at this spot', async () => {
    const { onOpenPdfs } = renderDetail('number:cand-101');
    await userEvent.click(screen.getByRole('button', { name: /Open PDFs/ }));
    expect(onOpenPdfs).toHaveBeenCalledWith(item('number:cand-101'));
  });

  it('a by-hand item offers right and wrong', async () => {
    const { onAct } = renderDetail('countertop:find-5');
    expect(screen.getByRole('button', { name: /It is right/ })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: /It is wrong/ }));
    expect(onAct).toHaveBeenCalledWith('checked_wrong');
  });

  it('a no-countertop page offers its two answers', () => {
    renderDetail('page:rec-9');
    expect(screen.getByRole('button', { name: /Confirm: no countertop/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /There is a countertop/ })).toBeTruthy();
  });

  it('Fix with no other reading says so and points to the PDFs', async () => {
    const { onOpenPdfs } = renderDetail('number:cand-102');
    await userEvent.click(screen.getByRole('button', { name: /Fix/ }));
    await userEvent.click(await screen.findByRole('menuitem', { name: /Open the PDFs/ }));
    expect(onOpenPdfs).toHaveBeenCalled();
  });

  it('a failed save is named, with Retry', async () => {
    const onAct = vi.fn();
    render(
      <PicturesProvider load={pictures}>
        <ItemDetail item={item('number:cand-101')} busy={null} error={{ message: 'The item changed since you opened it.', retry: onAct }} onAct={() => {}} onOpenPdfs={() => {}} />
      </PicturesProvider>,
    );
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('The item changed since you opened it.');
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(onAct).toHaveBeenCalled();
  });
});

describe('NotCheckedGroup', () => {
  it('is quiet and closed, says why, and clears all with one click', async () => {
    const onClearAll = vi.fn();
    render(<NotCheckedGroup items={data.not_checked} note={data.not_checked_note} busy={false} error={null} onClearAll={onClearAll} />);
    const toggle = screen.getByRole('button', { name: /Not checked/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.textContent).toContain('6');
    expect(screen.getByText(/the depth and sink reader is off/)).toBeTruthy();
    await userEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getAllByText('Countertop depth').length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('button', { name: 'Clear all as not checked' }));
    expect(onClearAll).toHaveBeenCalledTimes(1);
  });

  it('once cleared it says so, and offers nothing more', () => {
    const cleared = allSettled();
    render(<NotCheckedGroup items={cleared.not_checked} note={cleared.not_checked_note} busy={false} error={null} onClearAll={() => {}} />);
    expect(screen.queryByRole('button', { name: 'Clear all as not checked' })).toBeNull();
    expect(screen.getByText(/Cleared as not checked/)).toBeTruthy();
  });
});

describe('FinalBar', () => {
  it('stays locked while anything is open, and says why', () => {
    render(<FinalBar summary={summarize(data)} final={{ status: 'idle' }} onCreateFinal={() => {}} />);
    const button = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText('6 items still need you.')).toBeTruthy();
  });

  it('unlocks once everything is checked', async () => {
    const onCreateFinal = vi.fn();
    render(<FinalBar summary={summarize(allSettled())} final={{ status: 'idle' }} onCreateFinal={onCreateFinal} />);
    const button = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    await userEvent.click(button);
    expect(onCreateFinal).toHaveBeenCalled();
  });
});

describe('page states', () => {
  it('loading is a skeleton with a status for screen readers', () => {
    render(<ReviewSkeleton />);
    expect(screen.getByRole('status').textContent).toContain('Loading');
  });

  it('empty says nothing needs you', () => {
    render(<NothingNeedsYou checked={7} />);
    expect(screen.getByRole('status').textContent).toContain('Nothing needs you');
  });

  it('an error is named, with the request id, and Retry', async () => {
    const onRetry = vi.fn();
    render(<LoadError error={new ApiError(500, { error: 'boom', message: 'The review items could not be built.', request_id: 'req-7' })} onRetry={onRetry} />);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('The review items could not be built.');
    expect(alert.textContent).toContain('req-7');
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('records missing from the page show a red banner that opens the advanced view', async () => {
    const onOpenAdvanced = vi.fn();
    render(<UnaccountedBanner count={2} onOpenAdvanced={onOpenAdvanced} />);
    const banner = screen.getByRole('alert');
    expect(banner.textContent).toContain('2 records');
    await userEvent.click(within(banner).getByRole('button', { name: /advanced view/i }));
    expect(onOpenAdvanced).toHaveBeenCalled();
  });
});
