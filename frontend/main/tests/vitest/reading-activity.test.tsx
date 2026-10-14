// @vitest-environment jsdom
/**
 * The live reading activity (#1239): what each AI is doing while a package is read, when its
 * inspector stops it, and when it tries again, in plain words. The API shape is mocked here from the
 * design (`PackageOut.activity`, #1238); every value is made up.
 */
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ReadingActivity } from '@/components/review/reading-activity';
import { TooltipProvider } from '@/components/ui/tooltip';
import {
  activityLine,
  readActivity,
  readingHeadline,
  roleName,
  summaryParts,
  type ReadingActivityItemOut,
  type ReadingActivityOut,
} from '@/lib/reading-activity';

function item(over: Partial<ReadingActivityItemOut>): ReadingActivityItemOut {
  return { page: 12, agent: 'Rekha', item: 'p11:row-choice', state: 'stopped', code: 'readers-differ', change: null, line: 'a line from the server', at: '2026-10-14T10:00:00Z', ...over };
}

function activity(over: Partial<ReadingActivityOut> = {}): ReadingActivityOut {
  return {
    reading_id: '00000000-0000-4000-8000-000000000001',
    updated_at: '2026-10-14T10:00:05Z',
    now: 'Reading page 12 of 40',
    items: [],
    summary: { stopped: 0, tried_again: 0, helped: 0, waiting: 0 },
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

describe('reading activity: the words for every state (design §(c))', () => {
  it('reading: the page being read, from the progress line', () => {
    expect(readingHeadline({ pagesDone: 11, pagesTotal: 40 }, null)).toBe('Reading page 12 of 40…');
    // The last page is "40 of 40", never "41 of 40".
    expect(readingHeadline({ pagesDone: 40, pagesTotal: 40 }, null)).toBe('Reading page 40 of 40…');
    // No page count: the server's own line, else just "Reading…".
    expect(readingHeadline(null, 'Reading the countertop lines')).toBe('Reading the countertop lines…');
    expect(readingHeadline(null, null)).toBe('Reading…');
  });

  it('passed: the line finder and the number reader', () => {
    expect(activityLine(item({ state: 'passed', code: 'printed-numbers' }))).toBe('Page 12: countertop line found.');
    expect(activityLine(item({ agent: 'Akshara', item: 'p11:slot2:0', state: 'passed', code: 'agreed' }))).toBe('Page 12: numbers read and agreed.');
  });

  it('stopped: each reason in plain words', () => {
    expect(activityLine(item({ code: 'readers-differ' }))).toBe('Page 12: the two AIs picked different lines.');
    expect(activityLine(item({ code: 'no-printed-numbers' }))).toBe('Page 12: the chosen line has no printed numbers.');
    expect(activityLine(item({ agent: 'Akshara', code: 'readers-differ' }))).toBe('Page 12: the two AIs read one number differently.');
    expect(activityLine(item({ agent: 'Akshara', code: 'unsure' }))).toBe('Page 12: an AI was not sure about one number.');
    expect(activityLine(item({ agent: 'Akshara', code: 'unreadable' }))).toBe('Page 12: an AI could not read one number clearly.');
  });

  it('trying again: says what changed', () => {
    expect(activityLine(item({ state: 'trying_again', change: 'tie-break' }))).toBe("Couldn't settle the countertop line on page 12 — trying again with a closer look.");
    expect(activityLine(item({ agent: 'Akshara', state: 'trying_again', code: 'readers-differ', change: 'closer-look' }))).toBe('Trying again on a number on page 12 with a closer look.');
    expect(activityLine(item({ agent: 'Akshara', state: 'trying_again', code: 'readers-differ', change: 'zoom-3x' }))).toBe('Trying again on a number on page 12 with a 3× close-up.');
  });

  it('try-again result: helped, did not help, not tried', () => {
    expect(activityLine(item({ state: 'try_again_result', code: 'helped' }))).toBe('Trying again helped — both AIs now pick the same line, and it has printed numbers.');
    expect(activityLine(item({ agent: 'Akshara', state: 'try_again_result', code: 'helped' }))).toBe('Trying again helped — both AIs now read the number the same way.');
    expect(activityLine(item({ state: 'try_again_result', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to pick the line.");
    expect(activityLine(item({ agent: 'Akshara', state: 'try_again_result', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to check the number.");
    expect(activityLine(item({ state: 'try_again_result', code: 'allowance-used' }))).toBe('Not tried again — the try-again allowance for this set is used up. Waiting for you.');
  });

  it('waiting for you: a stop that was not tried again, or did not help', () => {
    expect(activityLine(item({ state: 'waiting', code: 'no-printed-numbers' }))).toBe('Page 12: the chosen line has no printed numbers. Waiting for you.');
    expect(activityLine(item({ state: 'waiting', code: 'not-helped' }))).toBe("Trying again didn't help — waiting for you to pick the line.");
  });

  it('a code this screen does not know yet: the server\'s own line, never a blank', () => {
    expect(activityLine(item({ code: 'some-new-code', line: 'Page 12: a new kind of stop.' }))).toBe('Page 12: a new kind of stop.');
    expect(activityLine(item({ agent: 'Milan', code: 'some-new-code', line: null }))).toBe('Page 12: the matcher stopped on one item. Waiting for you.');
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
    const row = screen.getByText('Page 12: the two AIs picked different lines.').closest('li')!;
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

  it('while reading: the page, then the newest happenings, in a polite live region', () => {
    show({
      activity: activity({
        items: [
          item({ page: 3, state: 'stopped', code: 'readers-differ', at: '2026-10-14T10:00:01Z' }),
          item({ page: 3, state: 'trying_again', change: 'tie-break', at: '2026-10-14T10:00:02Z' }),
          item({ page: 5, agent: 'Akshara', item: 'p4:slot1:0', state: 'try_again_result', code: 'not-helped', at: '2026-10-14T10:00:03Z' }),
        ],
        summary: { stopped: 2, tried_again: 2, helped: 0, waiting: 1 },
      }),
      reading: true,
      progress: { pagesDone: 11, pagesTotal: 40 },
    });
    const region = screen.getByRole('status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(within(region).getByText('Reading page 12 of 40…')).toBeTruthy();
    const rows = within(region).getAllByRole('listitem').map((li) => li.textContent);
    // Newest first.
    expect(rows[0]).toContain("Trying again didn't help — waiting for you to check the number.");
    expect(rows[0]).toContain('page 5');
    expect(rows[1]).toContain("Couldn't settle the countertop line on page 3 — trying again with a closer look.");
    expect(rows[2]).toContain('Page 3: the two AIs picked different lines.');
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
          <ReadingActivity activity={activity({ items: [item({ page: 1 })] })} reading progress={{ pagesDone: 1, pagesTotal: 9 }} />
        </TooltipProvider>,
      );
    });
    expect(screen.getByRole('status')).toBe(region);
    expect(region.textContent).toContain('Page 1: the two AIs picked different lines.');
  });
});

describe('reading activity: the summary after reading', () => {
  it('words the try-again counts', () => {
    expect(summaryParts({ stopped: 7, tried_again: 5, helped: 3, waiting: 4 })).toEqual({ lead: 'Tried again on 5 items: ', agreed: '3 now agree', waiting: '2 wait for you' });
    expect(summaryParts({ stopped: 1, tried_again: 1, helped: 1, waiting: 0 })).toEqual({ lead: 'Tried again on 1 item: ', agreed: '1 now agrees', waiting: null });
    expect(summaryParts({ stopped: 2, tried_again: 2, helped: 0, waiting: 2 })).toEqual({ lead: 'Tried again on 2 items: ', agreed: null, waiting: '2 wait for you' });
    expect(summaryParts({ stopped: 3, tried_again: 2, helped: 1, waiting: 1 })).toEqual({ lead: 'Tried again on 2 items: ', agreed: '1 now agrees', waiting: '1 waits for you' });
    expect(summaryParts({ stopped: 4, tried_again: 0, helped: 0, waiting: 4 })).toEqual({ lead: 'Reading finished: ', agreed: null, waiting: '4 items wait for you' });
    expect(summaryParts({ stopped: 0, tried_again: 0, helped: 0, waiting: 0 })).toBeNull();
  });

  it('shows the sentence with both counts linking to Decisions', async () => {
    const onOpenDecisions = vi.fn();
    show({
      activity: activity({ items: [item({})], summary: { stopped: 7, tried_again: 5, helped: 3, waiting: 4 } }),
      reading: false,
      onOpenDecisions,
    });
    const region = screen.getByRole('status');
    expect(region.textContent).toBe('Tried again on 5 items: 3 now agree, 2 wait for you.');
    // After reading, the summary replaces the running list.
    expect(within(region).queryAllByRole('listitem')).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: '3 now agree' }));
    await userEvent.click(screen.getByRole('button', { name: '2 wait for you' }));
    expect(onOpenDecisions).toHaveBeenCalledTimes(2);
  });

  it('with no try-again, says how many wait for you', () => {
    show({ activity: activity({ summary: { stopped: 4, tried_again: 0, helped: 0, waiting: 4 } }), reading: false, onOpenDecisions: () => {} });
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

  it('keeps well-formed items and drops broken ones', () => {
    const parsed = readActivity({ ...activity(), items: [item({}), { page: 'x' }, null] });
    expect(parsed?.items).toHaveLength(1);
    expect(readActivity({ ...activity(), summary: undefined })?.summary).toBeNull();
  });
});
