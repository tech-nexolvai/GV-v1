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
import { ChoiceCard } from '@/components/simple-review/choice-card';
import { PagesChoice } from '@/components/simple-review/pages-choice';
import type { FinalCloudApi } from '@/api/final-cloud';
import { LoadError, NothingNeedsYou, ReviewSkeleton, UnaccountedBanner } from '@/components/simple-review/states';
import { PicturesProvider } from '@/components/simple-review/item-pictures';
import { leftWords, summarize } from '@/lib/review-items';
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
    // A person's answer is not an outcome: never the green PASS glyph.
    expect(document.querySelector('[data-outcome-icon="PASS"]')).toBeNull();
    rerender(<ItemRow item={item('number:cand-120')} open={false} active={false} onToggle={() => {}} />);
    expect(screen.getByText('Re-checking')).toBeTruthy();
  });

  it('a missing page says so in words, never "p.null"', () => {
    render(<ItemRow item={{ ...item('number:cand-101'), page: null, countertop: null, row_label: null }} open={false} active={false} onToggle={() => {}} />);
    expect(screen.getByText('page not recorded')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/null|undefined/);
  });
});

describe('ItemDetail', () => {
  function renderDetail(key: string, extra: Partial<React.ComponentProps<typeof ItemDetail>> = {}) {
    const onAct = vi.fn();
    const onOpenPdfs = vi.fn();
    render(
      <PicturesProvider load={pictures}>
        <ItemDetail item={item(key)} busy={null} error={null} onAct={onAct} onOpenPdfs={onOpenPdfs} {...extra} />
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
    expect(screen.getByText('The stored reading; the readings beside it differ')).toBeTruthy();
    expect(screen.getByText('AI 2').parentElement?.textContent).toContain('96 3/4');
  });

  it('Confirm sends one click; Fix opens the dialog, which says plainly what is recorded (#1368)', async () => {
    const onFixOpenChange = vi.fn();
    const { onAct } = renderDetail('number:cand-101', { onFixOpenChange });
    await userEvent.click(screen.getByRole('button', { name: /Confirm/ }));
    expect(onAct).toHaveBeenCalledWith('confirm');
    await userEvent.click(screen.getByRole('button', { name: /Fix/ }));
    expect(onFixOpenChange).toHaveBeenCalledWith(true);
  });

  it('the Fix dialog takes what the drawing shows, in inches, as a correction (#1368)', async () => {
    const { onAct } = renderDetail('number:cand-101', { fixOpen: true });
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    expect(dialog.textContent).toContain("Type what the drawing shows, in inches. Recorded as a correction of the AI's reading.");
    const box = within(dialog).getByRole('textbox', { name: 'What the drawing shows' });
    const save = within(dialog).getByRole('button', { name: 'Save the correction' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    // The AI's own reading is not a correction: Confirm is the answer for that.
    await userEvent.type(box, '96 1/4');
    expect(save.disabled).toBe(true);
    expect(dialog.textContent).toContain('That is what the AI read');
    await userEvent.clear(box);
    await userEvent.type(box, '96 5/8');
    expect(dialog.textContent).toContain('Recorded as 96 5/8"');
    await userEvent.click(save);
    expect(onAct).toHaveBeenCalledWith('fix', { value: '96 5/8"' });
  });

  it("the Fix dialog offers another AI's reading as a quick fill, never sending it on its own", async () => {
    const { onAct } = renderDetail('number:cand-101', { fixOpen: true });
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    await userEvent.click(within(dialog).getByRole('button', { name: '96 3/4"' }));
    expect(onAct).not.toHaveBeenCalled();
    expect((within(dialog).getByRole('textbox') as HTMLInputElement).value).toBe('96 3/4');
    await userEvent.keyboard('{Enter}');
    expect(onAct).toHaveBeenCalledWith('fix', { value: '96 3/4"' });
  });

  it('a refused correction is shown inside the dialog, where it was typed', async () => {
    renderDetail('number:cand-101', { fixOpen: true, error: { message: 'Millimetres are not recorded.', retry: () => {} } });
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    expect(within(dialog).getByRole('alert').textContent).toContain('Millimetres are not recorded.');
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
    expect(screen.getByRole('button', { name: /No countertop here/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /There is a countertop/ })).toBeTruthy();
  });

  it('Fix with no other reading offers no quick fill, only the box', async () => {
    renderDetail('number:cand-102', { fixOpen: true });
    const dialog = await screen.findByRole('dialog', { name: 'Fix this number' });
    expect(within(dialog).queryByText('Another AI read')).toBeNull();
    expect(within(dialog).getByRole('textbox')).toBeTruthy();
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

describe('FinalBar (#1368)', () => {
  function finalApi(overrides: Partial<FinalCloudApi> = {}): FinalCloudApi {
    return {
      facts: vi.fn(async () => ({ signer: 'Sample Reviewer', carried: [] })),
      signOff: vi.fn(async () => {}),
      prepare: vi.fn(async () => 'preparing' as const),
      status: vi.fn(async () => 'ready' as const),
      download: vi.fn(async () => new Blob(['%PDF'])),
      ...overrides,
    };
  }
  function renderBar(source = data, api = finalApi(), approved = false) {
    const onSigned = vi.fn();
    render(<FinalBar summary={summarize(source)} left={leftWords(source)} api={api} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={approved} onSigned={onSigned} />);
    return { api, onSigned };
  }

  it("stays locked while the server says so: its reason, and what is left in counts", () => {
    renderBar();
    const button = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByRole('status').textContent).toBe('6 items still need you.');
    expect(screen.getByText('6 items to answer · 2 wrong results to agree · 6 checks not run · 1 item re-checking')).toBeTruthy();
  });

  it('follows the server even when the screen shows nothing open', () => {
    const settled = allSettled();
    settled.header.can_finalise = false;
    settled.header.blocked_reason = 'The checks are running again.';
    renderBar(settled);
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('status').textContent).toBe('The checks are running again.');
  });

  it('unlocked: confirms the sign-off first, then signs, makes the PDF and offers the download', async () => {
    let status: 'preparing' | 'ready' = 'preparing';
    const api = finalApi({ status: vi.fn(async () => status) });
    const { onSigned } = renderBar(allSettled(), api);
    await userEvent.click(screen.getByRole('button', { name: 'Create final cloud PDF' }));
    const dialog = await screen.findByRole('dialog', { name: 'Sign off and create the final cloud PDF?' });
    expect(await within(dialog).findByText('Sample Reviewer')).toBeTruthy();
    expect(dialog.textContent).toContain('This cannot be undone.');
    expect(api.signOff).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign off and create' }));
    expect(api.signOff).toHaveBeenCalledWith('proj-demo', 'pkg-demo', false);
    expect(onSigned).toHaveBeenCalled();
    // The PDF is asked for as its own step, after the sign-off is recorded.
    await waitFor(() => expect(api.prepare).toHaveBeenCalledWith('proj-demo', 'pkg-demo'));
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Making the final cloud PDF'));
    status = 'ready';
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download final cloud PDF' })).toBeTruthy(), { timeout: 5000 });
    const createObjectURL = vi.fn(() => 'blob:cloud');
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    await userEvent.click(screen.getByRole('button', { name: 'Download final cloud PDF' }));
    expect(api.download).toHaveBeenCalledWith('proj-demo', 'pkg-demo');
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it('a refused sign-off says nothing was signed, in the server\'s words', async () => {
    const api = finalApi({ signOff: vi.fn(async () => { throw new Error('2 results still need a decision.'); }) });
    renderBar(allSettled(), api);
    await userEvent.click(screen.getByRole('button', { name: 'Create final cloud PDF' }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('Sample Reviewer');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign off and create' }));
    expect((await within(dialog).findByRole('alert')).textContent).toBe('Nothing was signed: 2 results still need a decision.');
  });

  it('answers carried from an earlier revision must be ticked before signing', async () => {
    const api = finalApi({ facts: vi.fn(async () => ({ signer: null, carried: [{ rowId: 'row-1', label: 'Countertop 1', pageNumber: 3, words: 'walls at both ends', kind: 'wall' as const }] })) });
    renderBar(allSettled(), api);
    await userEvent.click(screen.getByRole('button', { name: 'Create final cloud PDF' }));
    const dialog = await screen.findByRole('dialog');
    const sign = await within(dialog).findByRole('button', { name: 'Sign off and create' }) as HTMLButtonElement;
    await waitFor(() => expect(sign.disabled).toBe(true));
    await userEvent.click(within(dialog).getByRole('checkbox', { name: 'These answers still apply to this revision' }));
    await userEvent.click(sign);
    expect(api.signOff).toHaveBeenCalledWith('proj-demo', 'pkg-demo', true);
  });

  it('a recorded sign-off whose PDF request fails says it was signed, and Try again only asks for the PDF', async () => {
    const prepare = vi.fn<FinalCloudApi['prepare']>()
      .mockRejectedValueOnce(new Error('The export service is busy.'))
      .mockResolvedValueOnce('preparing');
    const api = finalApi({ prepare, status: vi.fn(async () => 'preparing' as const) });
    const { onSigned } = renderBar(allSettled(), api);
    await userEvent.click(screen.getByRole('button', { name: 'Create final cloud PDF' }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('Sample Reviewer');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign off and create' }));
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Signed off, but'));
    expect(screen.getByText('Sign-off was recorded, but the final cloud PDF could not be made: The export service is busy. Try again.')).toBeTruthy();
    expect(onSigned).toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByText(/Nothing was signed/)).toBeNull();
    expect(screen.queryByRole('button', { name: /Sign off and create|Create final cloud PDF/ })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(prepare).toHaveBeenCalledTimes(2));
    expect(api.signOff).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Making the final cloud PDF'));
  });

  it('a set already signed off shows where its PDF is; a failed one offers Try again without signing again', async () => {
    const api = finalApi({ status: vi.fn(async () => 'failed' as const) });
    renderBar(allSettled(), api, true);
    expect(await screen.findByText('Failed: the final cloud PDF could not be made.')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(api.prepare).toHaveBeenCalledWith('proj-demo', 'pkg-demo');
    expect(api.signOff).not.toHaveBeenCalled();
  });
});

describe('ChoiceCard and PagesChoice (#1368)', () => {
  it('a choice says what it leads to; the proposal is pre-selected and labelled, never sent on its own', async () => {
    const onChoose = vi.fn();
    render(<ChoiceCard title="Walls at both ends" leadsTo="The width check runs again." proposal="the AI readers" onChoose={onChoose} />);
    // Not a pressed toggle: it is described as a proposal that is not saved until clicked.
    const card = screen.getByRole('button', { name: /Walls at both ends/, description: /Nothing is saved until you click\./ });
    expect(card.hasAttribute('aria-pressed')).toBe(false);
    const note = document.getElementById(card.getAttribute('aria-describedby')!)!;
    expect(note.textContent).toBe('Proposed by the AI readers. Nothing is saved until you click.');
    expect(note.closest('[aria-hidden="true"]')).toBeNull();
    // The name stays short: the note is the description, not part of the name.
    expect(card.getAttribute('aria-describedby')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Nothing is saved/ })).toBeNull();
    expect(card.textContent).toContain('AI proposal');
    expect(card.textContent).toContain('The width check runs again.');
    expect(onChoose).not.toHaveBeenCalled();
    await userEvent.click(card);
    expect(onChoose).toHaveBeenCalledTimes(1);
  });

  it('"These N pages have no countertop" is one click; any page can say "There is a countertop"', async () => {
    const pages = reviewItems((draft) => {
      const second = structuredClone(draft.items.find((entry) => entry.key === 'page:rec-9')!);
      second.key = 'page:rec-11';
      second.page = 11;
      draft.items.push(second);
    }).items.filter((entry) => entry.kind === 'no_countertop_page');
    const onConfirmAll = vi.fn();
    const onThereIsOne = vi.fn();
    const onLook = vi.fn();
    render(<PagesChoice pages={pages} busyKey={null} groupKey="group:pages" errors={{}} onConfirmAll={onConfirmAll} onThereIsOne={onThereIsOne} onLook={onLook} />);
    const all = screen.getByRole('button', { name: /These 2 pages have no countertop/ });
    expect(all.textContent).toContain('p.9 and p.11');
    expect(all.textContent).toContain('Nothing on them is checked');
    await userEvent.click(all);
    expect(onConfirmAll).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: 'There is a countertop on page 11' }));
    expect(onThereIsOne).toHaveBeenCalledWith(pages[1]);
    await userEvent.click(screen.getByRole('button', { name: 'Look at page 9' }));
    expect(onLook).toHaveBeenCalledWith(pages[0]);
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
    expect(document.querySelector('[data-outcome-icon]')).toBeNull();
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
