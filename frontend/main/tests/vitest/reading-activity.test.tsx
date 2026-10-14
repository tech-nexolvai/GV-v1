// @vitest-environment jsdom
/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words.
 *
 * The server owns every sentence (#1238 / PR #1242): each item carries `line` and `role`, the
 * activity carries `now` and `summary.sentence`. The screen shows them as they are, adds the role
 * tag (agent name on hover), a glyph by state and the links to Decisions. Its own wording is only a
 * fallback for a missing sentence. The fixtures follow #1242's posted shape; every value is made up.
 */
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ReadingActivity } from '@/components/review/reading-activity';
import { TooltipProvider } from '@/components/ui/tooltip';
import {
  activityLine,
  fallbackLine,
  linkedParts,
  readActivity,
  readingHeadline,
  roleTag,
  summarySentence,
  type ReadingActivityItemOut,
  type ReadingActivityOut,
  type ReadingActivitySummaryOut,
} from '@/lib/reading-activity';

/** Sentences as #1242 sends them. */
const SERVER = {
  stopped: 'Page 12: the line finder stopped: the two AIs picked different lines.',
  answered: 'Page 12: the line finder answered.',
  trying: 'Page 12: the number reader is trying again (try 2 of 3) with a 3× close-up, turned upright.',
  helped: 'Page 12: trying again helped — the two AIs now read the number the same way.',
  waiting: "Page 12: trying again didn't help — it waits for you.",
  now: 'Page 12: the number reader is trying again (try 2 of 3) with a 3× close-up, turned upright.',
  summary: 'Tried again on 2 items: 1 now agree; 1 item waits for you.',
};

function item(over: Partial<ReadingActivityItemOut>): ReadingActivityItemOut {
  return {
    page: 12, agent: 'Rekha', role: 'the line finder', item: 'p11:row-choice', state: 'stopped', code: 'readers-differ',
    change: null, attempt: null, line: SERVER.stopped, at: '2026-10-14T10:00:00Z', ...over,
  };
}

const NO_SUMMARY: ReadingActivitySummaryOut = { passed: 0, stopped: 0, tried_again: 0, helped: 0, waiting: 0, sentence: null };

function activity(over: Partial<ReadingActivityOut> = {}): ReadingActivityOut {
  return {
    reading_id: '00000000-0000-4000-8000-000000000001',
    updated_at: '2026-10-14T10:00:05Z',
    now: null,
    items: [],
    summary: NO_SUMMARY,
    ...over,
  };
}

/** #1242's posted example, as JSON from the wire. */
const POSTED = {
  reading_id: '00000000-0000-4000-8000-000000000002', updated_at: '2026-10-14T10:00:09Z', now: SERVER.now,
  items: [{
    page: 12, agent: 'Akshara', role: 'the number reader', item: 'p11:slot0:0', state: 'trying_again', code: 'readers-differ',
    change: 'closer-look-3x-upright', attempt: 2, line: SERVER.trying, at: '2026-10-14T10:00:09Z',
  }],
  summary: { passed: 41, stopped: 3, tried_again: 2, helped: 1, waiting: 1, sentence: SERVER.summary },
};

function show(props: Parameters<typeof ReadingActivity>[0]) {
  return render(
    <TooltipProvider>
      <ReadingActivity {...props} />
    </TooltipProvider>,
  );
}

describe('reading activity: the server owns every sentence (#1242)', () => {
  it.each([
    ['answered', item({ state: 'answered', line: SERVER.answered }), SERVER.answered],
    ['stopped', item({}), SERVER.stopped],
    ['trying again', item({ agent: 'Akshara', role: 'the number reader', state: 'trying_again', change: 'closer-look-3x-upright', attempt: 2, line: SERVER.trying }), SERVER.trying],
    ['helped', item({ agent: 'Akshara', role: 'the number reader', state: 'helped', line: SERVER.helped }), SERVER.helped],
    ['waiting', item({ state: 'waiting', code: 'not-helped', line: SERVER.waiting }), SERVER.waiting],
  ])('%s: the item\'s own line, word for word', (_, it_, expected) => {
    expect(activityLine(it_)).toBe(expected);
  });

  it('reading: the server\'s `now` first; without it, the page from the progress line', () => {
    expect(readingHeadline({ pagesDone: 3, pagesTotal: 9 }, SERVER.now)).toBe(SERVER.now);
    expect(readingHeadline({ pagesDone: 11, pagesTotal: 40 }, null)).toBe('Reading page 12 of 40…');
    expect(readingHeadline({ pagesDone: 40, pagesTotal: 40 }, '  ')).toBe('Reading page 40 of 40…');
    expect(readingHeadline(null, null)).toBe('Reading…');
  });

  it('the summary: the server\'s sentence first', () => {
    expect(summarySentence(POSTED.summary)).toBe(SERVER.summary);
  });

  it('reads #1242\'s posted example exactly', () => {
    expect(readActivity(POSTED)).toEqual(POSTED);
  });
});

describe('reading activity: the fallback when a sentence is missing', () => {
  it('a missing or empty line uses the screen\'s own words', () => {
    expect(activityLine(item({ line: null }))).toBe('Page 12: the two AIs picked different lines.');
    expect(activityLine(item({ line: '  ' }))).toBe('Page 12: the two AIs picked different lines.');
  });

  it('answered', () => {
    expect(fallbackLine(item({ state: 'answered', code: null }))).toBe('Page 12: the line finder answered.');
    // The server's role words are used where given.
    expect(fallbackLine(item({ agent: 'Somebody', role: 'the new helper', state: 'answered', code: null }))).toBe('Page 12: the new helper answered.');
  });

  it('stopped', () => {
    expect(fallbackLine(item({ code: 'no-printed-numbers' }))).toBe('Page 12: the chosen line has no printed numbers.');
    expect(fallbackLine(item({ agent: 'Akshara', code: 'readers-differ' }))).toBe('Page 12: the two AIs read one number differently.');
    expect(fallbackLine(item({ agent: 'Akshara', code: 'unsure' }))).toBe('Page 12: an AI was not sure about one number.');
    expect(fallbackLine(item({ agent: 'Akshara', code: 'unreadable' }))).toBe('Page 12: an AI could not read one number clearly.');
    expect(fallbackLine(item({ agent: 'Milan', role: null, code: 'some-new-code' }))).toBe('Page 12: the matcher stopped on one item. Waiting for you.');
  });

  it('trying again: each fixed change code (Anant: up to 3 tries, each changing something)', () => {
    const line = (change: string) => fallbackLine(item({ state: 'trying_again', change }));
    const number = (change: string) => fallbackLine(item({ agent: 'Akshara', state: 'trying_again', change }));
    expect(line('tie-break-disputed-lines')).toBe("Couldn't settle the countertop line on page 12 — trying again with a closer look at the disputed lines.");
    expect(line('other-numbered-lines')).toBe("Couldn't settle the countertop line on page 12 — trying again by showing the other numbered lines.");
    expect(line('each-line-alone')).toBe("Couldn't settle the countertop line on page 12 — trying again by showing each line alone.");
    expect(number('closer-look-2x')).toBe('Trying again on a number on page 12 with a 2× close-up.');
    expect(number('closer-look-3x-upright')).toBe('Trying again on a number on page 12 with a 3× close-up, turned upright.');
    expect(number('tighter-crop')).toBe('Trying again on a number on page 12 with a tighter crop.');
    // No guessing from the code's spelling: an unknown change says only that it is trying again.
    expect(number('zoom-4x')).toBe('Trying again on a number on page 12.');
  });

  it('helped', () => {
    expect(fallbackLine(item({ state: 'helped' }))).toBe('Trying again helped — both AIs now pick the same line, and it has printed numbers.');
    expect(fallbackLine(item({ agent: 'Akshara', state: 'helped' }))).toBe('Trying again helped — both AIs now read the number the same way.');
  });

  it('waiting for you: not helped, not tried, or a plain stop', () => {
    expect(fallbackLine(item({ state: 'waiting', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to pick the line.");
    expect(fallbackLine(item({ agent: 'Akshara', state: 'waiting', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to check the number.");
    expect(fallbackLine(item({ state: 'waiting', code: 'allowance-used' }))).toBe('Not tried again — the try-again allowance for this set is used up. Waiting for you.');
    expect(fallbackLine(item({ agent: 'Akshara', state: 'waiting', code: 'cannot-zoom' }))).toBe('Not tried again — the number cannot be shown any larger. Waiting for you.');
    expect(fallbackLine(item({ state: 'waiting', code: 'no-printed-numbers' }))).toBe('Page 12: the chosen line has no printed numbers. Waiting for you.');
  });

  it('the summary uses `summary.waiting` as it is (every item waiting for a person)', () => {
    const s = (over: Partial<ReadingActivitySummaryOut>) => summarySentence({ ...NO_SUMMARY, ...over });
    expect(s({ stopped: 7, tried_again: 5, helped: 3, waiting: 4 })).toBe('Tried again on 5 items: 3 now agree; 4 items wait for you.');
    expect(s({ stopped: 1, tried_again: 1, helped: 1 })).toBe('Tried again on 1 item: 1 now agree.');
    expect(s({ stopped: 2, tried_again: 2, waiting: 2, sentence: '' })).toBe('Tried again on 2 items: 2 items wait for you.');
    expect(s({ stopped: 4, waiting: 4 })).toBe('Reading finished: 4 items wait for you.');
    expect(s({ stopped: 1, waiting: 1 })).toBe('Reading finished: 1 item waits for you.');
    expect(s({ passed: 41 })).toBeNull();
    expect(summarySentence(null)).toBeNull();
  });
});

describe('reading activity: the counts in a sentence become links to Decisions', () => {
  it('splits out "N now agree" and "N item(s) wait(s) for you", exactly as #1242 writes them', () => {
    expect(linkedParts(SERVER.summary)).toEqual([
      { text: 'Tried again on 2 items: ', link: false },
      { text: '1 now agree', link: true },
      { text: '; ', link: false },
      { text: '1 item waits for you', link: true },
      { text: '.', link: false },
    ]);
    expect(linkedParts('Tried again on 5 items: 3 now agree; 2 items wait for you.').filter((p) => p.link).map((p) => p.text)).toEqual(['3 now agree', '2 items wait for you']);
    expect(linkedParts('Reading finished: 4 items wait for you.').filter((p) => p.link).map((p) => p.text)).toEqual(['4 items wait for you']);
    expect(linkedParts('Nothing to count.')).toEqual([{ text: 'Nothing to count.', link: false }]);
  });
});

describe('reading activity: role names on screen, agent names on hover (Anant, decision 1)', () => {
  it('the tag is the server\'s role; else the role this screen knows; never the agent', () => {
    expect(roleTag(item({ role: 'the number reader' }))).toBe('Number reader');
    expect(roleTag(item({ agent: 'Somebody', role: 'the new helper' }))).toBe('New helper');
    expect(roleTag(item({ agent: 'Rekha', role: null }))).toBe('Line finder');
    expect(roleTag(item({ agent: 'Akshara', role: '' }))).toBe('Number reader');
    expect(roleTag(item({ agent: 'Somebody', role: null }))).toBe('AI helper');
  });

  it('shows the role on the row and the agent name only on hover', async () => {
    show({ activity: activity({ items: [item({})] }), reading: true, progress: { pagesDone: 11, pagesTotal: 40 } });
    const row = screen.getByText(SERVER.stopped).closest('li')!;
    const role = within(row).getByText('Line finder');
    expect(row.textContent).not.toMatch(/Rekha/);
    await userEvent.hover(role);
    expect((await screen.findAllByText('Rekha')).length).toBeGreaterThan(0);
  });
});

describe('reading activity: the panel', () => {
  it('shows nothing when there is no activity', () => {
    const { container } = show({ activity: null, reading: true, progress: { pagesDone: 3, pagesTotal: 9 } });
    expect(container.textContent).toBe('');
    const absent = show({ activity: undefined, reading: false });
    expect(absent.container.textContent).toBe('');
  });

  it('while reading: `now`, then the server\'s lines newest first, with a glyph by state, in a polite live region', () => {
    show({
      activity: activity({
        now: SERVER.now,
        items: [
          item({ agent: 'Akshara', role: 'the number reader', state: 'waiting', code: 'not-helped', line: SERVER.waiting, at: '2026-10-14T10:00:05Z' }),
          item({ agent: 'Akshara', role: 'the number reader', state: 'helped', line: SERVER.helped, at: '2026-10-14T10:00:04Z' }),
          item({ agent: 'Akshara', role: 'the number reader', state: 'trying_again', change: 'closer-look-3x-upright', attempt: 2, line: SERVER.trying, at: '2026-10-14T10:00:03Z' }),
          item({ at: '2026-10-14T10:00:02Z' }),
          item({ state: 'answered', line: SERVER.answered, at: '2026-10-14T10:00:01Z' }),
        ],
      }),
      reading: true,
      progress: { pagesDone: 11, pagesTotal: 40 },
    });
    const region = screen.getByRole('status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(within(region).getAllByText(SERVER.now)[0].tagName).toBe('P');
    const rows = within(region).getAllByRole('listitem');
    // The newest 4, newest first, as the server ordered them.
    expect(rows.map((li) => li.getAttribute('data-state'))).toEqual(['waiting', 'helped', 'trying_again', 'stopped']);
    expect(rows.map((li) => li.querySelector('[data-glyph]')?.getAttribute('data-glyph'))).toEqual(['needs-you', 'done', 'trying-again', 'needs-you']);
    expect(rows[0].querySelector('[data-outcome-icon="REVIEW_REQUIRED"]')).not.toBeNull();
    expect(rows[0].textContent).toContain(SERVER.waiting);
    expect(rows[2].textContent).toContain(SERVER.trying);
    // A line that already names its page gets no second page tag.
    expect(rows.some((li) => li.textContent?.includes('· page'))).toBe(false);
  });

  it('an answered row has a plain tick; a line with no page gets one beside it', () => {
    show({ activity: activity({ items: [item({ state: 'answered', page: 4, line: 'The line finder answered.' })] }), reading: true });
    const row = screen.getByRole('listitem');
    expect(row.querySelector('[data-glyph]')?.getAttribute('data-glyph')).toBe('done');
    expect(row.textContent).toContain('The line finder answered. · page 4');
  });

  it('while reading with nothing stopped yet: only the page', () => {
    show({ activity: activity(), reading: true, progress: { pagesDone: 0, pagesTotal: 9 } });
    expect(screen.getByRole('status').textContent).toBe('Reading page 1 of 9…');
  });

  it('a new happening is announced in the same live region (no remount)', () => {
    const view = show({ activity: activity(), reading: true, progress: { pagesDone: 0, pagesTotal: 9 } });
    const region = screen.getByRole('status');
    act(() => {
      view.rerender(
        <TooltipProvider>
          <ReadingActivity activity={activity({ items: [item({ page: 1, line: 'Page 1: the line finder stopped: the two AIs picked different lines.' })] })} reading progress={{ pagesDone: 1, pagesTotal: 9 }} />
        </TooltipProvider>,
      );
    });
    expect(screen.getByRole('status')).toBe(region);
    expect(region.textContent).toContain('Page 1: the line finder stopped: the two AIs picked different lines.');
  });
});

describe('reading activity: the summary after reading', () => {
  it('shows the server\'s sentence with both counts linking to Decisions', async () => {
    const onOpenDecisions = vi.fn();
    show({ activity: readActivity({ ...POSTED, now: SERVER.summary }), reading: false, onOpenDecisions });
    const region = screen.getByRole('status');
    expect(region.textContent).toBe(SERVER.summary);
    // After reading, the summary replaces the running list.
    expect(within(region).queryAllByRole('listitem')).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: '1 now agree' }));
    await userEvent.click(screen.getByRole('button', { name: '1 item waits for you' }));
    expect(onOpenDecisions).toHaveBeenCalledTimes(2);
  });

  it('a sentence with no count to link still offers Decisions', async () => {
    const onOpenDecisions = vi.fn();
    show({ activity: activity({ summary: { ...NO_SUMMARY, stopped: 2, waiting: 2, sentence: 'Reading finished: two items are held.' } }), reading: false, onOpenDecisions });
    expect(screen.getByRole('status').textContent).toBe('Reading finished: two items are held. Open Decisions');
    await userEvent.click(screen.getByRole('button', { name: 'Open Decisions' }));
    expect(onOpenDecisions).toHaveBeenCalledTimes(1);
  });

  it('without a server sentence, the fallback wording', () => {
    show({ activity: activity({ summary: { ...NO_SUMMARY, stopped: 4, waiting: 4 } }), reading: false, onOpenDecisions: () => {} });
    expect(screen.getByRole('status').textContent).toBe('Reading finished: 4 items wait for you.');
  });

  it('shows nothing after a reading where nothing stopped (passes are only counted)', () => {
    const { container } = show({ activity: activity({ summary: { ...NO_SUMMARY, passed: 41 } }), reading: false });
    expect(container.textContent).toBe('');
  });
});

describe('reading activity: the API value is read tolerantly', () => {
  it('absent, null or a wrong shape is no activity', () => {
    expect(readActivity(undefined)).toBeNull();
    expect(readActivity(null)).toBeNull();
    expect(readActivity({ reading_id: 'x' })).toBeNull();
    expect(readActivity('text')).toBeNull();
  });

  it('keeps well-formed items and drops broken ones; odd fields become null', () => {
    const parsed = readActivity({ ...POSTED, items: [...POSTED.items, { page: 'x' }, null] });
    expect(parsed?.items).toHaveLength(1);
    expect(readActivity({ ...POSTED, summary: undefined })?.summary).toBeNull();
    expect(readActivity({ ...POSTED, items: [{ ...POSTED.items[0], attempt: 'two', line: 7, role: 3 }] })?.items[0]).toMatchObject({ attempt: null, line: null, role: null });
  });
});
