// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
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
import { leftWords, openNoCountertopPages, openPagesToDecide, summarize } from '@/lib/review-items';
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
    expect(line.textContent).toBe('AI read 12 pages · 148 numbers · 131 settled by both AIs (89%) · 17 not settled by the AIs');
    expect(within(line).getByText('148').className).toContain('num');
    expect(screen.getByRole('button', { name: /Open PDFs/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Draft cloud PDF/ })).toBeTruthy();
  });

  it("says how often the AI was right on the numbers you checked, all from the server (#1386)", () => {
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={() => {}} draft={{ status: 'idle' }} />);
    expect(screen.getByTestId('accuracy-line').textContent).toBe('Your answers so far: 12 · AI right on 11 (92%)');
    // A number both AIs settled that a person corrected stays in sight.
    expect(screen.getByTestId('settled-check-line').textContent).toBe('Of the numbers both AIs settled that you checked: 2 right of 3');
  });

  it('with nothing checked it shows no accuracy at all, never 0% or 100% (#1386)', () => {
    const header = { ...data.header, readings_checked: 0, ai_right: 0, ai_right_percent: null, settled_checked: 0, settled_right: 0 };
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={header} onOpenPdfs={() => {}} onDraftCloud={() => {}} draft={{ status: 'idle' }} />);
    expect(screen.getByTestId('accuracy-line').textContent).toBe('Accuracy: shown after you check some numbers');
    expect(screen.queryByTestId('settled-check-line')).toBeNull();
  });

  it('prints the percentages the server sent, never its own (#1386)', () => {
    // Deliberately not 131/148: the screen must print the server's number, not work one out.
    const header = { ...data.header, settled_percent: 7, ai_right_percent: 3 };
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={header} onOpenPdfs={() => {}} onDraftCloud={() => {}} draft={{ status: 'idle' }} />);
    expect(screen.getByTestId('ai-read-line').textContent).toContain('131 settled by both AIs (7%)');
    expect(screen.getByTestId('accuracy-line').textContent).toContain('AI right on 11 (3%)');
  });

  it('a draft being made says so; a failed one is named with Retry (#1394)', async () => {
    const onDraft = vi.fn();
    const { rerender } = render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={onDraft} draft={{ status: 'working' }} />);
    expect((screen.getByRole('button', { name: /Making draft/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('status').textContent).toContain('Making the draft cloud PDF');
    rerender(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={onDraft} draft={{ status: 'error', message: 'Run the checks first.' }} />);
    expect(screen.getByRole('alert').textContent).toBe('The draft cloud PDF could not be made: Run the checks first.');
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onDraft).toHaveBeenCalledTimes(1);
  });

  it('a draft that cannot be made yet is named, not hidden', () => {
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={null} draft={{ status: 'idle' }} />);
    const button = screen.getByRole('button', { name: /Draft cloud PDF/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toMatch(/cannot be asked for/i);
  });
});

describe('FailuresStrip', () => {
  it('lists each result to be clouded as wrong with a glyph, the word and one Agree', async () => {
    const onAgree = vi.fn();
    render(<FailuresStrip failures={data.failures} busyKey={null} errors={{}} onAgree={onAgree} onLook={() => {}} onSelect={() => {}} />);
    const strip = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    expect(within(strip).getAllByText('Wrong')).toHaveLength(2);
    expect(strip.querySelectorAll('[data-slot="failure-row"] [data-outcome-icon="FAIL"]')).toHaveLength(2);
    await userEvent.click(within(strip).getAllByRole('button', { name: /Agree/ })[0]);
    expect(onAgree).toHaveBeenCalledWith(data.failures[0]);
  });

  it('an agreed result says so and offers no Agree, only Look', () => {
    const agreed = reviewItems((draft) => { draft.failures[0].status = 'done'; draft.failures[0].actions = []; });
    render(<FailuresStrip failures={agreed.failures} busyKey={null} errors={{}} onAgree={() => {}} onLook={() => {}} onSelect={() => {}} />);
    expect(screen.getAllByRole('button', { name: /Agree/ })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /^Look at/ })).toHaveLength(2);
    expect(screen.getByText('Agreed')).toBeTruthy();
  });

  it('shows nothing when no result fails', () => {
    const { container } = render(<FailuresStrip failures={[]} busyKey={null} errors={{}} onAgree={() => {}} onLook={() => {}} onSelect={() => {}} />);
    expect(container.textContent).toBe('');
  });

  it('Look opens a row at its page without selecting it; a click on the row text selects it (#1388)', async () => {
    const onLook = vi.fn();
    const onSelect = vi.fn();
    const { rerender } = render(<FailuresStrip failures={data.failures} busyKey={null} errors={{}} onAgree={() => {}} onLook={onLook} onSelect={onSelect} />);
    const rows = screen.getAllByRole('listitem');
    await userEvent.click(within(rows[1]).getByRole('button', { name: /^Look at/ }));
    expect(onLook).toHaveBeenCalledWith(data.failures[1]);
    expect(onSelect).not.toHaveBeenCalled();
    await userEvent.click(within(rows[1]).getByText('Countertop 7'));
    expect(onSelect).toHaveBeenCalledWith(data.failures[1]);
    rerender(<FailuresStrip failures={data.failures} busyKey={null} errors={{}} selectedKey={data.failures[1].key} onAgree={() => {}} onLook={onLook} onSelect={onSelect} />);
    expect(rows[1].getAttribute('aria-current')).toBe('true');
    expect(rows[0].getAttribute('aria-current')).toBeNull();
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

  it('a countertop by hand says what to check: each piece, the overall and the rule', () => {
    // Made-up numbers: one piece read, one equal-share label, one under GV markup.
    const byHand = {
      ...item('countertop:find-5'),
      what_to_check: {
        row_reason: null,
        pieces: [
          { position: 1, kind: 'filler', value: '2"', mm: null, not_used: false, note: null },
          { position: 2, kind: 'cabinet', value: null, mm: null, not_used: false, note: 'written as 90"(5 EQ) — 5 equal pieces in one number' },
          { position: 3, kind: null, value: null, mm: null, not_used: false, note: 'under GV markup — check by hand' },
        ],
        overall: '96"',
        overall_note: null,
        rule: 'The pieces should add up to the overall: 96".',
        walls_note: 'Field cut recorded: 1" at each of 2 ends.',
      },
    };
    render(
      <PicturesProvider load={pictures}>
        <ItemDetail item={byHand} busy={null} error={null} onAct={() => {}} onOpenPdfs={() => {}} />
      </PicturesProvider>,
    );
    const block = screen.getByTestId('what-to-check');
    const rows = within(block).getAllByRole('listitem');
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('Piece 1');
    expect(rows[0].textContent).toContain('filler');
    expect(within(rows[0]).getByText('2"').className).toContain('num');
    expect(rows[1].textContent).toContain("Couldn't read");
    expect(rows[1].textContent).toContain('5 equal pieces in one number');
    expect(rows[2].textContent).toContain('under GV markup');
    expect(block.textContent).toContain('The pieces should add up to the overall: 96".');
    expect(block.textContent).toContain('Field cut recorded');
  });

  it('a row held as a whole says its reason once, and stored pieces show their inches, not used', () => {
    // Made-up numbers: a stored value with its mm, a piece with nothing stored.
    const rowReason =
      'This line includes a tall appliance (fridge or range), so it may measure wall to wall, not the countertop. The pieces below are not used for the check.';
    const byHand = {
      ...item('countertop:find-5'),
      why: 'This row includes a tall appliance or range bay; it may be the wall-to-wall line, not the countertop.',
      what_to_check: {
        row_reason: rowReason,
        pieces: [
          { position: 1, kind: 'filler', value: '10"', mm: '254 mm', not_used: true, note: null },
          { position: 2, kind: null, value: null, mm: null, not_used: false, note: 'no number was recorded for this piece' },
        ],
        overall: null,
        overall_note: null,
        rule: 'The overall for this row is not stored. Find the overall on the drawing.',
        walls_note: null,
      },
    };
    render(
      <PicturesProvider load={pictures}>
        <ItemDetail item={byHand} busy={null} error={null} onAct={() => {}} onOpenPdfs={() => {}} />
      </PicturesProvider>,
    );
    const block = screen.getByTestId('what-to-check');
    expect(within(block).getByTestId('what-to-check-row-reason').textContent).toBe(rowReason);
    const rows = within(block).getAllByRole('listitem');
    expect(rows[0].textContent).toContain('filler');
    expect(within(rows[0]).getByText('10"').className).toContain('num');
    expect(rows[0].textContent).toContain('254 mm');
    expect(rows[0].textContent).toContain('not used');
    expect(rows[0].textContent).not.toContain("Couldn't read");
    expect(rows[1].textContent).toContain("Couldn't read");
    expect(rows[1].textContent).toContain('no number was recorded for this piece');
    // The row's reason is said once on the screen, not again per piece or under the block.
    expect(screen.queryAllByText(/tall appliance/)).toHaveLength(1);
  });

  it('a countertop by hand with no close-up shows the row strip, not a blank box', async () => {
    renderDetail('countertop:find-5');
    expect(await screen.findByRole('img', { name: /The row on page 5/ })).toBeTruthy();
    expect(screen.getByRole('img', { name: /Vendor page 5, the row ringed/ })).toBeTruthy();
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

  it('links a check to the setting it waits for only when the API names one (#1373)', async () => {
    const withSettings = reviewItems((draft) => {
      draft.not_checked[0].settings = [{ key: 'cabinet_depth', name: 'Cabinet depth', project_id: 'proj-demo', set_now: false }];
      draft.not_checked[2].settings = [
        { key: 'cabinet_depth', name: 'Cabinet depth', project_id: 'proj-demo', set_now: false },
        { key: 'countertop_overhang', name: 'Countertop overhang', project_id: 'proj-demo', set_now: false },
      ];
    });
    render(<NotCheckedGroup items={withSettings.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: /Not checked/ }));
    const rows = screen.getAllByRole('listitem');
    // One setting: "Set it in Company settings", straight to that field of that project.
    const one = within(rows[0]).getByRole('link', { name: 'Set it in Company settings' });
    expect(one.getAttribute('href')).toBe('#/settings/company?focus=cabinet_depth&project=proj-demo');
    expect(rows[0].textContent).toContain('Cabinet depth');
    // No setting named (a drawn value, the sink card): no link, nothing guessed.
    expect(within(rows[1]).queryByRole('link')).toBeNull();
    // Several: one link each, named for its setting.
    const several = within(rows[2]).getAllByRole('link');
    expect(several.map((link) => link.getAttribute('href'))).toEqual([
      '#/settings/company?focus=cabinet_depth&project=proj-demo',
      '#/settings/company?focus=countertop_overhang&project=proj-demo',
    ]);
    expect(within(rows[2]).getByRole('link', { name: 'Set Countertop overhang in Company settings' })).toBeTruthy();
    expect(screen.getAllByRole('link')).toHaveLength(3);
  });

  it('says a setting saved since the run was set, with no link, and to run the checks again (#1382)', async () => {
    const setting = (key: string, name: string, set_now: boolean) => ({ key, name, project_id: 'proj-demo', set_now });
    const saved = reviewItems((draft) => {
      draft.not_checked[0].settings = [setting('cabinet_depth', 'Cabinet depth', true)];
      draft.not_checked[1].settings = [
        setting('cabinet_depth', 'Cabinet depth', true),
        setting('countertop_overhang', 'Countertop overhang', false),
      ];
      draft.not_checked[2].settings = [
        setting('cabinet_depth', 'Cabinet depth', true),
        setting('countertop_overhang', 'Countertop overhang', true),
      ];
    });
    render(<NotCheckedGroup items={saved.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: /Not checked/ }));
    const rows = screen.getAllByRole('listitem');
    // Set since the run: no link to a field that has a value, and the checks need running again.
    expect(within(rows[0]).queryByRole('link')).toBeNull();
    expect(rows[0].textContent).toContain('Cabinet depth was set after this check ran. Run the checks again.');
    expect(rows[0].textContent).not.toContain('Needs Cabinet depth');
    // One set, one not: the unset one keeps its link; no "run again" while one is still missing.
    const link = within(rows[1]).getByRole('link', { name: 'Set it in Company settings' });
    expect(link.getAttribute('href')).toBe('#/settings/company?focus=countertop_overhang&project=proj-demo');
    expect(rows[1].textContent).toContain('Needs Countertop overhang.');
    expect(rows[1].textContent).toContain('Cabinet depth was set after this check ran.');
    expect(rows[1].textContent).not.toContain('Run the checks again');
    // Both set: one sentence, no link.
    expect(within(rows[2]).queryByRole('link')).toBeNull();
    expect(rows[2].textContent).toContain(
      'Cabinet depth and Countertop overhang were set after this check ran. Run the checks again.',
    );
  });

  it('offers no setting link when the API names none', async () => {
    render(<NotCheckedGroup items={data.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: /Not checked/ }));
    expect(screen.queryAllByRole('link')).toHaveLength(0);
  });

  describe("this job's values (#1403)", () => {
    const setting = (key: string, name: string, set_now = false) => ({ key, name, project_id: 'proj-demo', set_now });
    const waiting = () =>
      reviewItems((draft) => {
        draft.not_checked[0].settings = [setting('cabinet_depth', 'Cabinet depth'), setting('countertop_overhang', 'Countertop overhang')];
        draft.not_checked[1].settings = [setting('countertop_overhang', 'Countertop overhang'), setting('backsplash_thickness', 'Backsplash thickness')];
        draft.not_checked[2].settings = [setting('cabinet_depth', 'Cabinet depth', true)];
      });

    it('says in one line how many are waiting for which values, with the full list folded', async () => {
      const listed = waiting();
      render(<NotCheckedGroup items={listed.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={() => {}} />);
      const summary = screen.getByTestId('job-values-summary');
      const counted = new Set(listed.not_checked.slice(0, 2).map((entry) => entry.countertop!.item_id)).size;
      expect(summary.textContent).toBe(
        `${counted} ${counted === 1 ? 'countertop is' : 'countertops are'} waiting for this job's 3 values: Cabinet depth · Countertop overhang · Backsplash thickness`,
      );
      expect(screen.queryAllByRole('listitem')).toHaveLength(0);
      await userEvent.click(screen.getByRole('button', { name: `Show all ${listed.not_checked.length}` }));
      expect(screen.getAllByRole('listitem')).toHaveLength(listed.not_checked.length);
    });

    it('counts a row on its page as a countertop, and checks when a result covers the whole set', () => {
      const rows = reviewItems((draft) => {
        draft.not_checked.forEach((entry, index) => {
          entry.countertop = null;
          entry.page = 2;
          entry.row_label = index < 3 ? 'Row A' : 'Row B';
          entry.settings = [setting('cabinet_depth', 'Cabinet depth')];
        });
      });
      const { unmount } = render(<NotCheckedGroup items={rows.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={() => {}} />);
      expect(screen.getByTestId('job-values-summary').textContent).toBe("2 countertops are waiting for this job's value: Cabinet depth");
      unmount();
      const whole = reviewItems((draft) => {
        draft.not_checked.forEach((entry, index) => {
          entry.countertop = null;
          entry.page = null;
          entry.row_label = null;
          entry.settings = index < 3 ? [setting('cabinet_depth', 'Cabinet depth')] : [];
        });
      });
      render(<NotCheckedGroup items={whole.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={() => {}} />);
      expect(screen.getByTestId('job-values-summary').textContent).toBe("3 checks are waiting for this job's value: Cabinet depth");
    });

    it('takes one inch value per missing value, refuses what is not inches, and saves only what was typed', async () => {
      const onSave = vi.fn();
      render(<NotCheckedGroup items={waiting().not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={onSave} />);
      await userEvent.click(screen.getByRole('button', { name: "Enter this job's values" }));
      expect(screen.getByText("From the drawings' sections or the GC's spec for this job.")).toBeTruthy();
      const fields = screen.getAllByRole('textbox');
      expect(fields).toHaveLength(3);
      await userEvent.type(screen.getByLabelText('Cabinet depth'), '24 mm');
      await userEvent.click(screen.getByRole('button', { name: 'Save and check again' }));
      expect(onSave).not.toHaveBeenCalled();
      expect(screen.getByText(/Cabinet depth: type inches, e.g. 1 1\/2/)).toBeTruthy();
      await userEvent.clear(screen.getByLabelText('Cabinet depth'));
      await userEvent.type(screen.getByLabelText('Cabinet depth'), '24');
      await userEvent.type(screen.getByLabelText('Countertop overhang'), '1 1/2"');
      await userEvent.click(screen.getByRole('button', { name: 'Save and check again' }));
      expect(onSave).toHaveBeenCalledWith([
        { name: 'cabinet_depth', value: '24"' },
        { name: 'countertop_overhang', value: '1 1/2"' },
      ]);
      // "Clear all as not checked" stays for values nobody knows.
      expect(screen.getByRole('button', { name: 'Clear all as not checked' })).toBeTruthy();
    });

    describe('what the job values reader found (#1409)', () => {
      const found = (name: string, value: string, auto: boolean, page = 55) => ({
        name,
        value,
        page,
        source: 'two AIs' as const,
        auto,
        location: { page_id: 'page-55', document_version_id: 'dv-arch', page_number: page, coordinate_space: 'stored', polygon: [['0.1', '0.2'], ['0.3', '0.2'], ['0.3', '0.4'], ['0.1', '0.4']] },
        note: '',
      });

      it('pre-fills an auto value, labels it, and saves it only on Save', async () => {
        const onSave = vi.fn();
        render(
          <NotCheckedGroup items={waiting().not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={onSave} suggestions={[found('backsplash_thickness', '3/4"', true)]} onLook={() => {}} />,
        );
        // The form opens with the value in view; nothing was sent.
        expect((screen.getByLabelText('Backsplash thickness') as HTMLInputElement).value).toBe('3/4');
        expect(screen.getByText(/Filled from p\.55 by two AIs and code/)).toBeTruthy();
        expect(onSave).not.toHaveBeenCalled();
        await userEvent.click(screen.getByRole('button', { name: 'Save and check again' }));
        expect(onSave).toHaveBeenCalledWith([{ name: 'backsplash_thickness', value: '3/4"' }]);
      });

      it('Change lets the person type over an auto value, and the label goes', async () => {
        render(
          <NotCheckedGroup items={waiting().not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={() => {}} suggestions={[found('backsplash_thickness', '3/4"', true)]} />,
        );
        await userEvent.click(screen.getByRole('button', { name: 'Change' }));
        expect(document.activeElement).toBe(screen.getByLabelText('Backsplash thickness'));
        await userEvent.keyboard('5/8');
        expect((screen.getByLabelText('Backsplash thickness') as HTMLInputElement).value).toBe('5/8');
        expect(screen.queryByText(/Filled from p\.55/)).toBeNull();
      });

      it('never pre-fills a plain suggestion: Found · page · Look · Use', async () => {
        const onLook = vi.fn();
        const onSave = vi.fn();
        const one = found('countertop_overhang', '1 1/2"', false, 12);
        render(
          <NotCheckedGroup items={waiting().not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={onSave} suggestions={[one]} onLook={onLook} />,
        );
        // Nothing auto: the form stays closed, and opens with the field empty.
        await userEvent.click(screen.getByRole('button', { name: "Enter this job's values" }));
        expect((screen.getByLabelText('Countertop overhang') as HTMLInputElement).value).toBe('');
        const line = document.querySelector('[data-part="job-value-found"]');
        expect(line?.textContent).toBe('Found 1 1/2" · p.12 · Look · Use');
        await userEvent.click(screen.getByRole('button', { name: 'Look' }));
        expect(onLook).toHaveBeenCalledWith(one, 'Countertop overhang');
        await userEvent.click(screen.getByRole('button', { name: 'Use 1 1/2"' }));
        expect((screen.getByLabelText('Countertop overhang') as HTMLInputElement).value).toBe('1 1/2');
        await userEvent.click(screen.getByRole('button', { name: 'Save and check again' }));
        expect(onSave).toHaveBeenCalledWith([{ name: 'countertop_overhang', value: '1 1/2"' }]);
      });
    });

    it('is read-only for someone who may not save the project values', () => {
      render(<NotCheckedGroup items={waiting().not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues={false} onSaveJobValues={() => {}} />);
      expect(screen.queryByRole('button', { name: "Enter this job's values" })).toBeNull();
      expect(screen.getByText(/Only this project's reviewers can enter its values/)).toBeTruthy();
    });

    it('offers no form when nothing waits for a value', () => {
      render(<NotCheckedGroup items={data.not_checked} note={null} busy={false} error={null} onClearAll={() => {}} canSaveJobValues onSaveJobValues={() => {}} />);
      expect(screen.queryByTestId('job-values-summary')).toBeNull();
      expect(screen.queryByRole('button', { name: "Enter this job's values" })).toBeNull();
    });
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
    render(<FinalBar summary={summarize(source)} left={leftWords(source.header)} api={api} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={approved} onSigned={onSigned} />);
    return { api, onSigned };
  }

  it("stays locked while the server says so, in plain words with the header's counts (#1394)", () => {
    const technical = reviewItems((draft) => {
      draft.header.open = 9;
      draft.header.not_checked = 54;
      draft.header.rechecking = 0;
      draft.header.blocked_reason = '61 findings still need a valid reviewer decision or a check rerun after correction.';
    });
    renderBar(technical);
    const button = screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByRole('status').textContent).toBe('9 items need you · 54 not checked');
    expect(screen.queryByText(/valid reviewer decision/)).toBeNull();
  });

  it('under StrictMode the sign-off dialog still names the signer and can be confirmed (#1394)', async () => {
    const api = finalApi();
    render(
      <StrictMode>
        <FinalBar summary={summarize(allSettled())} left={[]} api={api} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={false} onSigned={vi.fn()} />
      </StrictMode>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Create final cloud PDF' }));
    const dialog = await screen.findByRole('dialog', { name: 'Sign off and create the final cloud PDF?' });
    expect(await within(dialog).findByText('Sample Reviewer')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Sign off and create' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('follows the server even when the screen shows nothing open', () => {
    const settled = allSettled();
    settled.header.can_finalise = false;
    settled.header.blocked_reason = 'The checks are running again.';
    renderBar(settled);
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    // No count says what is left, so the server's own reason explains it.
    expect(screen.getByRole('status').textContent).toBe('Not ready for sign-off yet.');
    expect(screen.getByText('The checks are running again.')).toBeTruthy();
  });

  it("says the server's blocked reason and offers Run the checks again when inputs changed since the last run (QA 2026-10-16)", async () => {
    const stale = allSettled();
    stale.header.can_finalise = false;
    stale.header.blocked_reason = 'You changed inputs after the last check run. Run the checks before signing off.';
    const runChecks = vi.fn(async () => {});
    const onRechecked = vi.fn();
    render(<FinalBar summary={summarize(stale)} left={leftWords(stale.header)} api={finalApi({ runChecks })} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={false} onSigned={vi.fn()} onRechecked={onRechecked} />);
    expect(screen.getByRole('status').textContent).toBe('The checks need to run again.');
    expect(screen.getByText('You changed inputs after the last check run. Run the checks before signing off.')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Run the checks again' }));
    expect(runChecks).toHaveBeenCalledWith('proj-demo', 'pkg-demo');
    await waitFor(() => expect(onRechecked).toHaveBeenCalled());
  });

  it('shows the re-run reason beside the counts too, and says why when asking for the run fails', async () => {
    const stale = reviewItems((draft) => {
      draft.items = draft.items.filter((item) => item.status !== 'rechecking');
      draft.header.open = 2;
      draft.header.not_checked = 0;
      draft.header.rechecking = 0;
      draft.header.can_finalise = false;
      draft.header.blocked_reason = 'You changed inputs after the last check run. Run the checks before signing off.';
    });
    const runChecks = vi.fn(async () => {
      throw new Error('The checks are already running.');
    });
    renderBar(stale, finalApi({ runChecks }));
    expect(screen.getByRole('status').textContent).toBe('2 items need you');
    expect(screen.getByText(/You changed inputs after the last check run/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Run the checks again' }));
    expect((await screen.findByRole('alert')).textContent).toContain('The checks are already running.');
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
    expect(all.textContent).toContain('Will be marked as no countertop: p.9 and p.11');
    expect(all.textContent).not.toContain('Confirmed for');
    expect(all.textContent).toContain('Nothing on them is checked');
    await userEvent.click(all);
    expect(onConfirmAll).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: 'There is a countertop on page 11' }));
    expect(onThereIsOne).toHaveBeenCalledWith(pages[1]);
    await userEvent.click(screen.getByRole('button', { name: 'Look at page 9' }));
    expect(onLook).toHaveBeenCalledWith(pages[0]);
  });
});

describe('pages to decide are never in the one-click proposal (#1394)', () => {
  it('an older reading is listed apart as "Decide this page", with both answers and nothing proposed', async () => {
    const pages = reviewItems((draft) => {
      const older = structuredClone(draft.items.find((entry) => entry.key === 'page:rec-9')!);
      older.key = 'page:rec-4';
      older.page = 4;
      older.what = 'Decide this page';
      older.proposed_no_countertop = false;
      draft.items.push(older);
    }).items;
    const proposed = openNoCountertopPages(pages);
    const toDecide = openPagesToDecide(pages);
    expect(proposed.map((page) => page.key)).toEqual(['page:rec-9']);
    expect(toDecide.map((page) => page.key)).toEqual(['page:rec-4']);
    const onNoCountertop = vi.fn();
    const onThereIsOne = vi.fn();
    render(<PagesChoice pages={proposed} toDecide={toDecide} busyKey={null} groupKey="group:pages" errors={{}} onConfirmAll={vi.fn()} onThereIsOne={onThereIsOne} onNoCountertop={onNoCountertop} onLook={vi.fn()} />);
    const all = screen.getByRole('button', { name: /Page 9 has no countertop/ });
    expect(all.textContent).not.toContain('p.4');
    const decide = document.querySelector('[data-slot="pages-to-decide"]')!;
    expect(decide.textContent).toContain('Decide this page');
    expect(decide.textContent).not.toContain('AI proposal');
    await userEvent.click(screen.getByRole('button', { name: 'No countertop on page 4' }));
    expect(onNoCountertop).toHaveBeenCalledWith(toDecide[0]);
    await userEvent.click(screen.getByRole('button', { name: 'There is a countertop on page 4' }));
    expect(onThereIsOne).toHaveBeenCalledWith(toDecide[0]);
  });

  it('renders when no page is proposed at all: every page is "Decide this page" (live set 1 crash)', () => {
    const pages = reviewItems((draft) => {
      const page = draft.items.find((entry) => entry.key === 'page:rec-9')!;
      page.proposed_no_countertop = false;
    }).items;
    const toDecide = openPagesToDecide(pages);
    expect(openNoCountertopPages(pages)).toEqual([]);
    render(<PagesChoice pages={[]} toDecide={toDecide} busyKey={null} groupKey="group:pages" errors={{}} onConfirmAll={vi.fn()} onThereIsOne={vi.fn()} onNoCountertop={vi.fn()} onLook={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /have no countertop|has no countertop/ })).toBeNull();
    expect(document.querySelector('[data-slot="pages-to-decide"]')!.textContent).toContain('Decide this page');
  });

  it('a page with no proposal flag is never proposed', () => {
    const pages = reviewItems((draft) => {
      delete (draft.items.find((entry) => entry.key === 'page:rec-9') as { proposed_no_countertop?: boolean }).proposed_no_countertop;
    }).items;
    expect(openNoCountertopPages(pages)).toEqual([]);
    expect(openPagesToDecide(pages).map((page) => page.key)).toEqual(['page:rec-9']);
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

  it('never says nothing needs you while sign-off is blocked (QA 2026-10-16)', () => {
    render(<NothingNeedsYou checked={7} blocked="You changed inputs after the last check run. Run the checks before signing off." />);
    const status = screen.getByRole('status').textContent ?? '';
    expect(status).not.toContain('Nothing needs you');
    expect(status).toContain('Nothing to answer');
    expect(status).toContain('You changed inputs after the last check run.');
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

describe('CabinetKinds (#648)', () => {
  const cabinetItem = () => ({
    ...item('countertop:find-5'),
    key: 'cabinets:find-cab',
    kind: 'cabinet_kinds' as const,
    what: 'Which cabinets keep their width?',
    status: 'open' as const,
    actions: ['mark_cabinets'],
    finding_id: 'find-cab',
    page: null,
    best_guess: null,
    cabinets: [
      { position: 1, architect_width: '24"', vendor_width: '24"', marked: null, keeps_width: null },
      { position: 2, architect_width: '36"', vendor_width: '36"', marked: null, keeps_width: null },
    ],
    cabinet_choices: [
      { category: 'single_door', label: 'Single door', keeps_width: false },
      { category: 'drawer', label: 'Drawer', keeps_width: false },
      { category: 'sink_cabinet', label: 'Sink cabinet', keeps_width: true },
    ],
    settings: [{ key: 'drawer_cab_width_min', name: 'Drawer cabinet minimum width', project_id: 'proj-1', set_now: false }],
  });

  it('marks each cabinet as keeping its width or resizable, and saves once', async () => {
    const onAct = vi.fn();
    render(<ItemDetail item={cabinetItem()} busy={null} error={null} onAct={onAct} onOpenPdfs={() => {}} />);
    const save = screen.getByRole('button', { name: /Save marks/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    // No agree or disagree button: the marks are the answer.
    expect(screen.queryByRole('button', { name: /It is right/ })).toBeNull();

    const first = screen.getByRole('group', { name: 'Cabinet 1' });
    await userEvent.click(within(first).getByRole('button', { name: /Can be resized/ }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Cabinet 1: which kind' }), 'single_door');
    const second = screen.getByRole('group', { name: 'Cabinet 2' });
    await userEvent.click(within(second).getByRole('button', { name: /Keeps its width/ }));
    const kinds = screen.getByRole('combobox', { name: 'Cabinet 2: which kind' });
    expect(within(kinds).queryByRole('option', { name: 'Drawer' })).toBeNull();
    await userEvent.selectOptions(kinds, 'sink_cabinet');

    expect(save.disabled).toBe(false);
    await userEvent.click(save);
    expect(onAct).toHaveBeenCalledWith('mark_cabinets', { cabinet_kinds: ['single_door', 'sink_cabinet'] });
  });

  it('links a missing width limit to its Company settings field', () => {
    render(<ItemDetail item={cabinetItem()} busy={null} error={null} onAct={() => {}} onOpenPdfs={() => {}} />);
    expect(screen.getByText(/Not checked: set in Company settings/)).toBeTruthy();
    const link = screen.getByRole('link', { name: /Set it in Company settings/ }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('focus=drawer_cab_width_min');
  });
});

describe('screen polish (2026-10-16)', () => {
  it('the page buttons wrap and shrink, so "There is a countertop" is never clipped on a phone', () => {
    const pages = reviewItems((draft) => {
      const page = draft.items.find((entry) => entry.key === 'page:rec-9')!;
      const older = structuredClone(page);
      older.key = 'page:rec-4';
      older.page = 4;
      older.proposed_no_countertop = false;
      draft.items.push(older);
    }).items;
    render(<PagesChoice pages={openNoCountertopPages(pages)} toDecide={openPagesToDecide(pages)} busyKey={null} groupKey="group:pages" errors={{}} onConfirmAll={vi.fn()} onThereIsOne={vi.fn()} onNoCountertop={vi.fn()} onLook={vi.fn()} />);
    const rows = document.querySelectorAll('[data-slot="page-actions"]');
    expect(rows.length).toBe(2);
    for (const actions of rows) {
      const classes = actions.className.split(/\s+/);
      expect(classes).toContain('flex-wrap');
      expect(classes).toContain('min-w-0');
      expect(classes).toContain('max-w-full');
      expect(classes).not.toContain('shrink-0');
      expect(actions.textContent).toContain('There is a countertop');
    }
  });

  it('after sign-off the draft button is off and says to download the final cloud PDF instead', async () => {
    const onDraft = vi.fn();
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={onDraft} draft={{ status: 'idle' }} signedOff />);
    const button = screen.getByRole('button', { name: /Draft cloud PDF/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByTestId('draft-signed-note').textContent).toBe('Signed off — download the final cloud PDF instead');
    await userEvent.click(button);
    expect(onDraft).not.toHaveBeenCalled();
  });

  it('before sign-off the draft button works and no note is shown', () => {
    render(<ReviewHeader title="Sample Stoneworks" revision={2} header={data.header} onOpenPdfs={() => {}} onDraftCloud={() => {}} draft={{ status: 'idle' }} signedOff={false} />);
    expect((screen.getByRole('button', { name: /Draft cloud PDF/ }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId('draft-signed-note')).toBeNull();
  });

  const api: FinalCloudApi = {
    facts: vi.fn(async () => ({ signer: 'Sample Reviewer', carried: [] })),
    signOff: vi.fn(async () => {}),
    prepare: vi.fn(async () => 'preparing' as const),
    status: vi.fn(async () => 'ready' as const),
    download: vi.fn(async () => new Blob(['%PDF'])),
  };
  function onlyNotChecked(canFinalise: boolean) {
    return reviewItems((draft) => {
      draft.items = draft.items.filter((entry) => entry.status === 'done');
      draft.failures = [];
      draft.header = { ...draft.header, open: 0, failures: 0, not_checked: 54, rechecking: 0, can_finalise: canFinalise, blocked_reason: canFinalise ? null : 'Sample reason from the server.' };
    });
  }

  it('only not-checked left and the server still locks: says they are printed NOT CHECKED, never "unlocks once these are settled"', () => {
    const source = onlyNotChecked(false);
    render(<FinalBar summary={summarize(source)} left={leftWords(source.header)} notChecked={source.header.not_checked} api={api} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={false} onSigned={vi.fn()} />);
    expect(screen.getByRole('status').textContent).toBe('54 not checked — printed NOT CHECKED on the PDF');
    expect(screen.queryByText(/unlocks once these are settled/)).toBeNull();
    // Readiness is still the server's: locked, with its own words.
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Sample reason from the server.')).toBeTruthy();
  });

  it('only not-checked left and the server unlocks: the same words, and sign-off is offered', () => {
    const source = onlyNotChecked(true);
    render(<FinalBar summary={summarize(source)} left={leftWords(source.header)} notChecked={source.header.not_checked} api={api} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={false} onSigned={vi.fn()} />);
    expect(screen.getByRole('status').textContent).toBe('54 not checked — printed NOT CHECKED on the PDF');
    expect(screen.queryByText(/unlocks once these are settled/)).toBeNull();
    expect((screen.getByRole('button', { name: 'Create final cloud PDF' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('items that need a person still say "unlocks once these are settled"', () => {
    const source = reviewItems((draft) => {
      draft.header = { ...draft.header, open: 3, not_checked: 5, rechecking: 0, can_finalise: false };
    });
    render(<FinalBar summary={summarize(source)} left={leftWords(source.header)} notChecked={source.header.not_checked} api={api} projectId="proj-demo" packageId="pkg-demo" revision={2} approved={false} onSigned={vi.fn()} />);
    expect(screen.getByRole('status').textContent).toBe('3 items need you · 5 not checked');
    expect(screen.getByText('The final cloud PDF unlocks once these are settled.')).toBeTruthy();
  });
});
