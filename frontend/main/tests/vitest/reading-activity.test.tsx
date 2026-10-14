// @vitest-environment jsdom
/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words.
 *
 * The server owns every sentence (#1238, merged as #1242). The sentences below are copied from its
 * builders in `app/runs/reading_activity.py` (`event_line`, `summary_sentence`, `DID_NOT_FINISH`),
 * so these tests see what the screen really receives. Every value is made up.
 */
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ReadingActivity } from '@/components/review/reading-activity';
import { TooltipProvider } from '@/components/ui/tooltip';
import {
  activityGlyph,
  activityLine,
  afterReadingSentence,
  readingHeadline,
  roleTag,
  whatStopped,
  type ReadingActivityItemOut,
  type ReadingActivityOut,
  type ReadingActivitySummaryOut,
} from '@/lib/reading-activity';

/** The backend's own sentences (`app/runs/reading_activity.py`). */
const SERVER = {
  lineStopped: "Page 12: the line finder's two AIs picked different lines.",
  noNumbers: "Page 14: the line finder's chosen line has no printed numbers.",
  numberStopped: "Page 13: the number reader's two AIs read a number differently.",
  budget: 'Page 3: not read: the AI budget was reached before this page.',
  answered: "Page 9: the line finder's AIs answered.",
  trying: 'Page 12: the number reader is trying again (try 2 of 3) with a 3× close-up, turned upright.',
  helped: "Page 12: try 1 helped; the line finder's two AIs now agree.",
  waiting: "Page 12: try 3 didn't help; it waits for you.",
  summaryTried: 'Tried again on 2 items: 1 now agrees; 1 item waits for you.',
  summaryWaits: 'From the reading, 3 items wait for you.',
  summaryNothing: 'From the reading, nothing waits for you.',
  didNotFinish: 'The last reading did not finish, so what it did is not shown.',
};

function item(over: Partial<ReadingActivityItemOut>): ReadingActivityItemOut {
  return {
    page: 12, agent: 'Rekha', role: 'the line finder', item: 'p11:row-choice', state: 'stopped', code: 'readers-differ',
    change: null, attempt: null, line: SERVER.lineStopped, at: '2026-10-14T10:00:00Z', ...over,
  };
}

const NO_SUMMARY: ReadingActivitySummaryOut = { passed: 0, stopped: 0, tried_again: 0, helped: 0, waiting: 0, sentence: SERVER.summaryNothing };

function activity(over: Partial<ReadingActivityOut> = {}): ReadingActivityOut {
  return {
    reading_id: '00000000-0000-4000-8000-000000000001',
    finished: true,
    updated_at: '2026-10-14T10:00:05Z',
    now: '',
    items: [],
    summary: NO_SUMMARY,
    ...over,
  };
}

const NUMBER = { agent: 'Akshara', role: 'the number reader', item: 'p11:slot0:0' } as const;
const SORTER = { agent: 'Bala', role: 'the page sorter', item: 'p2:page' } as const;

function show(props: Parameters<typeof ReadingActivity>[0]) {
  return render(
    <TooltipProvider>
      <ReadingActivity {...props} />
    </TooltipProvider>,
  );
}

describe('reading activity: the server owns every sentence (#1242)', () => {
  it.each([
    ['answered', item({ state: 'answered', code: 'answered', page: 9, line: SERVER.answered }), SERVER.answered],
    ['stopped (line finder)', item({}), SERVER.lineStopped],
    ['stopped (number reader)', item({ ...NUMBER, page: 13, line: SERVER.numberStopped }), SERVER.numberStopped],
    ['stopped (page sorter)', item({ ...SORTER, page: 3, code: 'budget-reached', line: SERVER.budget }), SERVER.budget],
    ['trying again', item({ ...NUMBER, state: 'trying_again', change: 'closer-look-3x-upright', attempt: 2, line: SERVER.trying }), SERVER.trying],
    ['helped', item({ state: 'helped', code: 'helped', attempt: 1, line: SERVER.helped }), SERVER.helped],
    ['waiting', item({ state: 'waiting', code: 'not-helped', attempt: 3, line: SERVER.waiting }), SERVER.waiting],
  ])('%s: the item\'s own line, word for word', (_, it_, expected) => {
    expect(activityLine(it_)).toBe(expected);
  });

  it('an empty sentence shows the item\'s code plainly (the screen writes no sentences of its own)', () => {
    expect(activityLine(item({ line: '' }))).toBe('Page 12: readers-differ');
    expect(activityLine(item({ line: '  ', code: 'budget-reached', page: 3 }))).toBe('Page 3: budget-reached');
  });

  it('the glyph by state', () => {
    expect(activityGlyph({ state: 'stopped' })).toBe('needs-you');
    expect(activityGlyph({ state: 'waiting' })).toBe('needs-you');
    expect(activityGlyph({ state: 'trying_again' })).toBe('trying-again');
    expect(activityGlyph({ state: 'helped' })).toBe('done');
    expect(activityGlyph({ state: 'answered' })).toBe('done');
  });

  it('after reading: `summary.sentence`, else `now`, else nothing', () => {
    expect(afterReadingSentence(activity({ summary: { ...NO_SUMMARY, sentence: SERVER.summaryTried } }))).toBe(SERVER.summaryTried);
    expect(afterReadingSentence(activity({ now: SERVER.summaryWaits, summary: { ...NO_SUMMARY, sentence: '' } }))).toBe(SERVER.summaryWaits);
    expect(afterReadingSentence(activity({ now: ' ', summary: { ...NO_SUMMARY, sentence: '' } }))).toBeNull();
  });
});

describe('reading activity: the headline while reading', () => {
  it('the server\'s `now`; without it, the page from the progress line', () => {
    expect(readingHeadline({ pagesDone: 3, pagesTotal: 9 }, SERVER.numberStopped)).toBe(SERVER.numberStopped);
    expect(readingHeadline({ pagesDone: 11, pagesTotal: 40 }, '')).toBe('Reading page 12 of 40…');
    expect(readingHeadline({ pagesDone: 40, pagesTotal: 40 }, '  ')).toBe('Reading page 40 of 40…');
    expect(readingHeadline(null, null)).toBe('Reading…');
  });

  it('when `now` repeats ANY shown row, the page count instead (#1240 review)', () => {
    const rows = [SERVER.waiting, SERVER.trying, SERVER.lineStopped];
    expect(readingHeadline({ pagesDone: 11, pagesTotal: 40 }, SERVER.waiting, rows)).toBe('Reading page 12 of 40…');
    expect(readingHeadline({ pagesDone: 11, pagesTotal: 40 }, SERVER.lineStopped, rows)).toBe('Reading page 12 of 40…');
    expect(readingHeadline(null, `${SERVER.trying} `, rows)).toBe('Reading…');
    expect(readingHeadline({ pagesDone: 11, pagesTotal: 40 }, SERVER.numberStopped, rows)).toBe(SERVER.numberStopped);
  });

  it('the panel never says `now` twice, wherever its row is', () => {
    show({
      activity: activity({ finished: false, now: SERVER.lineStopped, items: [item({ ...NUMBER, line: SERVER.trying, state: 'trying_again' }), item({})] }),
      reading: true,
      progress: { pagesDone: 11, pagesTotal: 40 },
    });
    const region = screen.getByRole('status');
    expect(within(region).getByText('Reading page 12 of 40…').tagName).toBe('P');
    expect(within(region).getAllByText(SERVER.lineStopped)).toHaveLength(1);
  });
});

describe('reading activity: role names on screen, agent names on hover (Anant, decision 1)', () => {
  it('the tag is the server\'s role without its article; never the agent', () => {
    expect(roleTag({ role: 'the number reader' })).toBe('Number reader');
    expect(roleTag({ role: 'the line finder' })).toBe('Line finder');
    expect(roleTag({ role: 'the page sorter' })).toBe('Page sorter');
    expect(roleTag({ role: '' })).toBe('AI helper');
  });

  it('shows the role on the row and the agent name only on hover', async () => {
    show({ activity: activity({ finished: false, items: [item({})] }), reading: true, progress: { pagesDone: 11, pagesTotal: 40 } });
    const row = screen.getByText(SERVER.lineStopped).closest('li')!;
    const role = within(row).getByText('Line finder');
    expect(row.textContent).not.toMatch(/Rekha/);
    await userEvent.hover(role);
    expect((await screen.findAllByText('Rekha')).length).toBeGreaterThan(0);
  });
});

describe('reading activity: while reading', () => {
  it('shows nothing when there is no activity (null before the attempt\'s first event)', () => {
    const { container } = show({ activity: null, reading: true, progress: { pagesDone: 3, pagesTotal: 9 } });
    expect(container.textContent).toBe('');
    const absent = show({ activity: undefined, reading: false });
    expect(absent.container.textContent).toBe('');
  });

  it('the server\'s items in its order (never re-sorted), each with a glyph by state, in a polite live region', () => {
    show({
      activity: activity({
        finished: false,
        now: SERVER.waiting,
        items: [
          item({ state: 'waiting', code: 'not-helped', attempt: 3, line: SERVER.waiting, at: '2026-10-14T10:00:05Z' }),
          item({ ...SORTER, page: 3, code: 'budget-reached', line: SERVER.budget, at: '2026-10-14T10:00:01Z' }),
          item({ ...NUMBER, state: 'trying_again', change: 'closer-look-3x-upright', attempt: 2, line: SERVER.trying, at: '2026-10-14T10:00:03Z' }),
          // Answered later in time, but the server put it last: it only fills a leftover place.
          item({ state: 'answered', code: 'answered', page: 9, line: SERVER.answered, at: '2026-10-14T10:00:09Z' }),
          item({ state: 'helped', code: 'helped', attempt: 1, line: SERVER.helped }),
        ],
      }),
      reading: true,
      progress: { pagesDone: 11, pagesTotal: 40 },
    });
    const region = screen.getByRole('status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    const rows = within(region).getAllByRole('listitem');
    expect(rows.map((li) => li.getAttribute('data-state'))).toEqual(['waiting', 'stopped', 'trying_again', 'answered']);
    expect(rows.map((li) => li.querySelector('[data-glyph]')?.getAttribute('data-glyph'))).toEqual(['needs-you', 'needs-you', 'trying-again', 'done']);
    expect(rows[1].textContent).toContain(SERVER.budget);
    expect(within(rows[1]).getByText('Page sorter')).toBeTruthy();
    expect(rows[1].textContent).not.toContain('Bala');
    // Lines that name their page get no second page tag.
    expect(rows.some((li) => li.textContent?.includes('· page'))).toBe(false);
  });

  it('a line that does not name its page gets the page beside it', () => {
    show({ activity: activity({ finished: false, items: [item({ page: 4, line: 'The line finder answered.', state: 'answered' })] }), reading: true });
    expect(screen.getByRole('listitem').textContent).toContain('The line finder answered. · page 4');
  });

  it('two items with the same page and question id (two files) both show', () => {
    show({ activity: activity({ finished: false, items: [item({}), item({})] }), reading: true });
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('with nothing yet but the progress: only the page', () => {
    show({ activity: activity({ finished: false }), reading: true, progress: { pagesDone: 0, pagesTotal: 9 } });
    expect(screen.getByRole('status').textContent).toBe('Reading page 1 of 9…');
  });

  it('a new happening is announced in the same live region (no remount)', () => {
    const view = show({ activity: activity({ finished: false }), reading: true, progress: { pagesDone: 0, pagesTotal: 9 } });
    const region = screen.getByRole('status');
    const next = "Page 1: the line finder's two AIs picked different lines.";
    act(() => {
      view.rerender(
        <TooltipProvider>
          <ReadingActivity activity={activity({ finished: false, items: [item({ page: 1, line: next })] })} reading progress={{ pagesDone: 1, pagesTotal: 9 }} />
        </TooltipProvider>,
      );
    });
    expect(screen.getByRole('status')).toBe(region);
    expect(region.textContent).toContain(next);
  });
});

describe('reading activity: after a finished reading', () => {
  it('the server\'s sentence, an "Open Decisions" link that claims no number, and the list of what stopped', async () => {
    const onOpenDecisions = vi.fn();
    const waits = item({ state: 'waiting', code: 'not-helped', attempt: 3, line: SERVER.waiting });
    show({
      activity: activity({
        now: SERVER.summaryTried,
        items: [item({ state: 'helped', code: 'helped', attempt: 1, line: SERVER.helped }), waits],
        summary: { passed: 41, stopped: 2, tried_again: 2, helped: 1, waiting: 1, sentence: SERVER.summaryTried },
      }),
      reading: false,
      onOpenDecisions,
    });
    const region = screen.getByRole('status');
    expect(region.textContent).toBe(`${SERVER.summaryTried} Open Decisions`);
    // No count in the sentence is a link: only "Open Decisions" is.
    expect(within(region).getAllByRole('button').map((b) => b.textContent)).toEqual(['Open Decisions']);
    await userEvent.click(screen.getByRole('button', { name: 'Open Decisions' }));
    expect(onOpenDecisions).toHaveBeenCalledTimes(1);

    // The stop reasons stay reachable: collapsed, with every waiting item and its role tag.
    const list = document.querySelector('[data-slot="what-stopped"]') as HTMLDetailsElement;
    expect(list.open).toBe(false);
    expect(list.querySelector('summary')?.textContent).toBe('Show what stopped (1)');
    const rows = within(list).getAllByRole('listitem', { hidden: true });
    expect(rows.map((li) => li.textContent)).toEqual([`${SERVER.waiting}Line finder`]);
    expect(list.textContent).not.toContain('showing');
  });

  it('"Show what stopped (N)" says honestly when the API capped the list', () => {
    const items = Array.from({ length: 20 }, (_, n) => item({ page: n + 1, line: `Page ${n + 1}: the line finder's two AIs picked different lines.` }));
    show({ activity: activity({ items, summary: { ...NO_SUMMARY, stopped: 27, waiting: 27, sentence: 'From the reading, 27 items wait for you.' } }), reading: false, onOpenDecisions: () => {} });
    const list = document.querySelector('[data-slot="what-stopped"]') as HTMLElement;
    expect(list.querySelector('summary')?.textContent).toBe('Show what stopped (27)');
    expect(list.textContent).toContain('showing 20 of 27');
    expect(within(list).getAllByRole('listitem', { hidden: true })).toHaveLength(20);
  });

  it('nothing waits: the server\'s "nothing waits for you", with no link and no list', () => {
    show({ activity: activity({ now: SERVER.summaryNothing, summary: { ...NO_SUMMARY, passed: 41 } }), reading: false, onOpenDecisions: () => {} });
    expect(screen.getByRole('status').textContent).toBe(SERVER.summaryNothing);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(document.querySelector('[data-slot="what-stopped"]')).toBeNull();
  });

  it('with an empty summary sentence, the server\'s `now`', () => {
    show({ activity: activity({ now: SERVER.summaryWaits, summary: { ...NO_SUMMARY, stopped: 3, waiting: 3, sentence: '' } }), reading: false, onOpenDecisions: () => {} });
    expect(screen.getByRole('status').textContent).toBe(`${SERVER.summaryWaits} Open Decisions`);
  });

  it('shows nothing only when the server said nothing', () => {
    const { container } = show({ activity: activity({ now: '', summary: { ...NO_SUMMARY, sentence: '' } }), reading: false });
    expect(container.textContent).toBe('');
  });
});

describe('reading activity: after a reading that did not finish', () => {
  it('its sentence plainly, with no counts, links, glyph or list', () => {
    show({ activity: activity({ finished: false, now: SERVER.didNotFinish, summary: { ...NO_SUMMARY, sentence: SERVER.didNotFinish } }), reading: false, onOpenDecisions: () => {} });
    const region = screen.getByRole('status');
    expect(region.textContent).toBe(SERVER.didNotFinish);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(region.querySelector('[data-outcome-icon]')).toBeNull();
    expect(document.querySelector('[data-slot="what-stopped"]')).toBeNull();
    expect(document.querySelector('[data-slot="reading-activity"]')?.getAttribute('data-finished')).toBe('false');
  });
});

describe('reading activity: what stopped', () => {
  it('lists only the items that wait (stopped or waiting), and counts `summary.waiting`', () => {
    const got = whatStopped(activity({
      items: [item({ state: 'helped', line: SERVER.helped }), item({}), item({ state: 'waiting' }), item({ state: 'answered' })],
      summary: { ...NO_SUMMARY, waiting: 5 },
    }));
    expect(got.items.map((i) => i.state)).toEqual(['stopped', 'waiting']);
    expect(got.total).toBe(5);
  });
});
