// @vitest-environment jsdom
/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words.
 *
 * The server owns every sentence (#1238): each item carries `line`, the activity carries `now` and
 * `summary.sentence`. The screen shows them as they are, adds the role tag (agent name on hover), a
 * glyph by state and the links to Decisions. Its own wording is only a fallback for a missing
 * sentence. The API shape is mocked here from the design; every value is made up.
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
  roleName,
  summarySentence,
  type ReadingActivityItemOut,
  type ReadingActivityOut,
  type ReadingActivitySummaryOut,
} from '@/lib/reading-activity';

/** Sentences as the server sends them (fixtures in the design's §(c) wording). */
const SERVER = {
  stoppedLines: 'Page 12: the line finder stopped: the two AIs picked different lines.',
  stoppedNoNumbers: 'Page 12: the chosen line has no printed numbers.',
  stoppedNumber: 'Page 5: the two AIs read one number differently.',
  tryingLines: "Couldn't settle the countertop line on page 12 — trying again with a closer look at the disputed lines (try 1 of 3).",
  tryingNumber: 'Trying again on a number on page 5 with a 3× close-up, turned upright (try 2 of 3).',
  helped: 'Trying again helped — both AIs now pick the same line, and it has printed numbers.',
  notHelped: "Trying again didn't help — waiting for you to pick the line.",
  allowance: 'Not tried again — the try-again allowance for this set is used up. Waiting for you.',
  passed: 'Page 12: countertop line found.',
  now: 'Reading page 12 of 40…',
  summary: 'Tried again on 5 items: 3 now agree, 4 wait for you.',
};

function item(over: Partial<ReadingActivityItemOut>): ReadingActivityItemOut {
  return { page: 12, agent: 'Rekha', item: 'p11:row-choice', state: 'stopped', code: 'readers-differ', change: null, attempt: null, line: SERVER.stoppedLines, at: '2026-10-14T10:00:00Z', ...over };
}

const NO_SUMMARY: ReadingActivitySummaryOut = { stopped: 0, tried_again: 0, helped: 0, waiting: 0, sentence: null };

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

function show(props: Parameters<typeof ReadingActivity>[0]) {
  return render(
    <TooltipProvider>
      <ReadingActivity {...props} />
    </TooltipProvider>,
  );
}

describe('reading activity: the server owns every sentence (#1238)', () => {
  it.each([
    ['stopped', item({}), SERVER.stoppedLines],
    ['stopped (no printed numbers)', item({ code: 'no-printed-numbers', line: SERVER.stoppedNoNumbers }), SERVER.stoppedNoNumbers],
    ['stopped (number reader)', item({ page: 5, agent: 'Akshara', item: 'p4:slot1:0', line: SERVER.stoppedNumber }), SERVER.stoppedNumber],
    ['trying again (line finder)', item({ state: 'trying_again', change: 'tie-break-disputed-lines', attempt: 1, line: SERVER.tryingLines }), SERVER.tryingLines],
    ['trying again (number reader)', item({ page: 5, agent: 'Akshara', state: 'trying_again', change: 'closer-look-3x-upright', attempt: 2, line: SERVER.tryingNumber }), SERVER.tryingNumber],
    ['helped', item({ state: 'try_again_result', code: 'helped', line: SERVER.helped }), SERVER.helped],
    ["didn't help", item({ state: 'try_again_result', code: 'not-helped', line: SERVER.notHelped }), SERVER.notHelped],
    ['allowance used', item({ state: 'try_again_result', code: 'allowance-used', line: SERVER.allowance }), SERVER.allowance],
    ['passed', item({ state: 'passed', code: 'printed-numbers', line: SERVER.passed }), SERVER.passed],
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
    expect(summarySentence({ stopped: 7, tried_again: 5, helped: 3, waiting: 4, sentence: SERVER.summary })).toBe(SERVER.summary);
  });
});

describe('reading activity: the fallback when a sentence is missing', () => {
  it('a missing or empty line uses the screen\'s own words', () => {
    expect(activityLine(item({ line: null }))).toBe('Page 12: the two AIs picked different lines.');
    expect(activityLine(item({ line: '  ' }))).toBe('Page 12: the two AIs picked different lines.');
  });

  it('stopped', () => {
    expect(fallbackLine(item({ code: 'no-printed-numbers' }))).toBe('Page 12: the chosen line has no printed numbers.');
    expect(fallbackLine(item({ agent: 'Akshara', code: 'readers-differ' }))).toBe('Page 12: the two AIs read one number differently.');
    expect(fallbackLine(item({ agent: 'Akshara', code: 'unsure' }))).toBe('Page 12: an AI was not sure about one number.');
    expect(fallbackLine(item({ agent: 'Akshara', code: 'unreadable' }))).toBe('Page 12: an AI could not read one number clearly.');
    expect(fallbackLine(item({ agent: 'Milan', code: 'some-new-code' }))).toBe('Page 12: the matcher stopped on one item. Waiting for you.');
  });

  it('passed', () => {
    expect(fallbackLine(item({ state: 'passed', code: 'printed-numbers' }))).toBe('Page 12: countertop line found.');
    expect(fallbackLine(item({ agent: 'Akshara', state: 'passed', code: 'agreed' }))).toBe('Page 12: numbers read and agreed.');
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

  it('try-again results', () => {
    expect(fallbackLine(item({ state: 'try_again_result', code: 'helped' }))).toBe('Trying again helped — both AIs now pick the same line, and it has printed numbers.');
    expect(fallbackLine(item({ agent: 'Akshara', state: 'try_again_result', code: 'helped' }))).toBe('Trying again helped — both AIs now read the number the same way.');
    expect(fallbackLine(item({ state: 'try_again_result', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to pick the line.");
    expect(fallbackLine(item({ agent: 'Akshara', state: 'try_again_result', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to check the number.");
    expect(fallbackLine(item({ state: 'try_again_result', code: 'allowance-used' }))).toBe('Not tried again — the try-again allowance for this set is used up. Waiting for you.');
    expect(fallbackLine(item({ agent: 'Akshara', state: 'try_again_result', code: 'cannot-zoom' }))).toBe('Not tried again — the number cannot be shown any larger. Waiting for you.');
  });

  it('waiting for you', () => {
    expect(fallbackLine(item({ state: 'waiting', code: 'no-printed-numbers' }))).toBe('Page 12: the chosen line has no printed numbers. Waiting for you.');
    expect(fallbackLine(item({ state: 'waiting', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to pick the line.");
  });

  it('the summary uses `summary.waiting` as it is (every item waiting for a person)', () => {
    expect(summarySentence({ stopped: 7, tried_again: 5, helped: 3, waiting: 4, sentence: null })).toBe('Tried again on 5 items: 3 now agree, 4 wait for you.');
    expect(summarySentence({ stopped: 1, tried_again: 1, helped: 1, waiting: 0, sentence: null })).toBe('Tried again on 1 item: 1 now agrees.');
    expect(summarySentence({ stopped: 2, tried_again: 2, helped: 0, waiting: 2, sentence: '' })).toBe('Tried again on 2 items: 2 wait for you.');
    expect(summarySentence({ stopped: 3, tried_again: 2, helped: 1, waiting: 1, sentence: null })).toBe('Tried again on 2 items: 1 now agrees, 1 waits for you.');
    expect(summarySentence({ stopped: 4, tried_again: 0, helped: 0, waiting: 4, sentence: null })).toBe('Reading finished: 4 items wait for you.');
    expect(summarySentence(NO_SUMMARY)).toBeNull();
    expect(summarySentence(null)).toBeNull();
  });
});

describe('reading activity: the counts in a sentence become links to Decisions', () => {
  it('splits out "N now agree" and "N wait for you"', () => {
    expect(linkedParts(SERVER.summary)).toEqual([
      { text: 'Tried again on 5 items: ', link: false },
      { text: '3 now agree', link: true },
      { text: ', ', link: false },
      { text: '4 wait for you', link: true },
      { text: '.', link: false },
    ]);
    expect(linkedParts('Reading finished: 1 item waits for you.')).toEqual([
      { text: 'Reading finished: ', link: false },
      { text: '1 item waits for you', link: true },
      { text: '.', link: false },
    ]);
    expect(linkedParts('Nothing to count.')).toEqual([{ text: 'Nothing to count.', link: false }]);
  });
});

describe('reading activity: role names on screen, agent names on hover (Anant, decision 1)', () => {
  it('names the role, never the agent', () => {
    expect(roleName('Rekha')).toBe('Line finder');
    expect(roleName('Akshara')).toBe('Number reader');
    expect(roleName('Somebody')).toBe('AI helper');
  });

  it('shows the role on the row and the agent name only on hover', async () => {
    show({ activity: activity({ items: [item({})] }), reading: true, progress: { pagesDone: 11, pagesTotal: 40 } });
    const row = screen.getByText(SERVER.stoppedLines).closest('li')!;
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

  it('while reading: `now`, then the newest server lines with a glyph by state, in a polite live region', () => {
    show({
      activity: activity({
        now: SERVER.now,
        items: [
          item({ page: 3, line: 'Page 3: the two AIs picked different lines.', at: '2026-10-14T10:00:01Z' }),
          item({ page: 3, state: 'trying_again', change: 'tie-break-disputed-lines', attempt: 1, line: SERVER.tryingLines, at: '2026-10-14T10:00:02Z' }),
          item({ page: 5, agent: 'Akshara', item: 'p4:slot1:0', state: 'try_again_result', code: 'not-helped', line: "Trying again didn't help — waiting for you to check the number.", at: '2026-10-14T10:00:03Z' }),
          item({ page: 6, state: 'try_again_result', code: 'helped', line: SERVER.helped, at: '2026-10-14T10:00:00Z' }),
        ],
      }),
      reading: true,
      progress: { pagesDone: 11, pagesTotal: 40 },
    });
    const region = screen.getByRole('status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(within(region).getByText(SERVER.now)).toBeTruthy();
    const rows = within(region).getAllByRole('listitem');
    // Newest first, each with the server's line and a glyph that matches its state.
    expect(rows.map((li) => li.getAttribute('data-state'))).toEqual(['try_again_result', 'trying_again', 'stopped', 'try_again_result']);
    expect(rows[0].textContent).toContain("Trying again didn't help — waiting for you to check the number.");
    expect(rows[0].textContent).toContain('page 5');
    expect(rows[0].querySelector('[data-outcome-icon="REVIEW_REQUIRED"]')).not.toBeNull();
    expect(rows[1].textContent).toContain(SERVER.tryingLines);
    expect(rows[1].querySelector('[data-glyph="trying-again"]')).not.toBeNull();
    expect(rows[2].querySelector('[data-outcome-icon="REVIEW_REQUIRED"]')).not.toBeNull();
    expect(rows[3].querySelector('[data-glyph="done"]')).not.toBeNull();
    // A line that already names its page gets no second page tag.
    expect(rows[2].textContent).not.toContain('· page');
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
          <ReadingActivity activity={activity({ items: [item({ page: 1, line: 'Page 1: the two AIs picked different lines.' })] })} reading progress={{ pagesDone: 1, pagesTotal: 9 }} />
        </TooltipProvider>,
      );
    });
    expect(screen.getByRole('status')).toBe(region);
    expect(region.textContent).toContain('Page 1: the two AIs picked different lines.');
  });
});

describe('reading activity: the summary after reading', () => {
  it('shows the server\'s sentence with both counts linking to Decisions', async () => {
    const onOpenDecisions = vi.fn();
    show({
      activity: activity({ items: [item({})], summary: { stopped: 7, tried_again: 5, helped: 3, waiting: 4, sentence: SERVER.summary } }),
      reading: false,
      onOpenDecisions,
    });
    const region = screen.getByRole('status');
    expect(region.textContent).toBe(SERVER.summary);
    // After reading, the summary replaces the running list.
    expect(within(region).queryAllByRole('listitem')).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: '3 now agree' }));
    await userEvent.click(screen.getByRole('button', { name: '4 wait for you' }));
    expect(onOpenDecisions).toHaveBeenCalledTimes(2);
  });

  it('a sentence with no count to link still offers Decisions', async () => {
    const onOpenDecisions = vi.fn();
    show({ activity: activity({ summary: { stopped: 2, tried_again: 0, helped: 0, waiting: 2, sentence: 'Reading finished: two items are held.' } }), reading: false, onOpenDecisions });
    expect(screen.getByRole('status').textContent).toBe('Reading finished: two items are held. Open Decisions');
    await userEvent.click(screen.getByRole('button', { name: 'Open Decisions' }));
    expect(onOpenDecisions).toHaveBeenCalledTimes(1);
  });

  it('without a server sentence, the fallback wording', () => {
    show({ activity: activity({ summary: { stopped: 4, tried_again: 0, helped: 0, waiting: 4, sentence: null } }), reading: false, onOpenDecisions: () => {} });
    expect(screen.getByRole('status').textContent).toBe('Reading finished: 4 items wait for you.');
  });

  it('shows nothing after a reading where nothing stopped', () => {
    const { container } = show({ activity: activity(), reading: false });
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

  it('keeps well-formed items and drops broken ones; reads `attempt` and `summary.sentence`', () => {
    const parsed = readActivity({ ...activity({ summary: { ...NO_SUMMARY, waiting: 2, sentence: SERVER.summary } }), items: [item({ attempt: 2 }), { page: 'x' }, null] });
    expect(parsed?.items).toHaveLength(1);
    expect(parsed?.items[0].attempt).toBe(2);
    expect(parsed?.summary).toEqual({ stopped: 0, tried_again: 0, helped: 0, waiting: 2, sentence: SERVER.summary });
    expect(readActivity({ ...activity(), summary: undefined })?.summary).toBeNull();
    expect(readActivity({ ...activity(), items: [{ ...item({}), attempt: 'two', line: 7 }] })?.items[0]).toMatchObject({ attempt: null, line: null });
  });
});
