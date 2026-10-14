// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { SlotReaderRow } from '@/api/client';
import { SlotReaderRows } from '@/components/measure/SlotReaderRows';
import { wallQuestion } from '@/lib/needs-you-queue';

// Synthetic data only (#1241): nothing here comes from a client drawing.
const HOLD =
  'Synthetic Reviewer chose the walls (back wall and left end) for the countertop row on page 3 on an earlier reading, ' +
  'but that answer could not be carried to the new reading because its row is not provably the same row. ' +
  'Please choose the walls again for this row.';

const HELD: SlotReaderRow = {
  row_id: 'row-3',
  page_number: 3,
  label: 'Countertop row on page 3',
  piece_count: 2,
  held_reason: null,
  wall_confirmation_allowed: true,
  values: [],
  wall_proposal: null,
  wall_source: 'code-ink-and-both-ais',
  wall_reason: HOLD,
  wall_layout_choices: ['back_left_right', 'back_and_left', 'back_and_right', 'back_only', 'island'],
  decision_id: null,
  confirmed_by: null,
  decided_at: null,
  wall_config: null,
  walls_needed: true,
  wall_evidence: [],
  wall_hold_reason: HOLD,
};

const rows = vi.hoisted(() => ({ current: [] as SlotReaderRow[] }));

vi.mock('@/api/config', () => ({ projectId: () => 'p' }));
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listSlotReaderRows: vi.fn(async () => ({ rows: rows.current })),
  getCountertopResults: vi.fn(async () => ({ items: [] })),
}));

describe('a wall answer a newer reading could not carry (#1241)', () => {
  it('says so on the row, above the wall choices, and asks the walls again', async () => {
    rows.current = [HELD];
    render(<SlotReaderRows packageId="pkg" refresh={0} />);
    const hold = await screen.findByText((_text, element) => element?.getAttribute('data-part') === 'wall-hold');
    expect(hold.textContent).toContain('Walls asked again');
    expect(hold.textContent).toContain(HOLD);
    const choices = screen.getByText('Wall layout for this row');
    // Above the choices: the reason is read before the question.
    expect(hold.compareDocumentPosition(choices) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Said once, not again under the choices.
    expect(document.body.textContent?.split(HOLD).length).toBe(2);
    // Nothing reads as automatic walls on a held row.
    expect(screen.queryByText(/This is the row's check input/)).toBeNull();
    // The queue asks it too, with the same reason, and never as automatic.
    const question = wallQuestion(HELD);
    expect(question?.reason).toBe(HOLD);
    expect(question?.automatic).toBe(false);
  });

  it('does not ask the walls again on a held row whose result is the same for every wall case', async () => {
    rows.current = [{ ...HELD, row_id: 'row-5', walls_needed: false }];
    render(<SlotReaderRows packageId="pkg" refresh={0} />);
    await waitFor(() => expect(screen.getByText(/Walls not needed/)).toBeTruthy());
    // No "please choose the walls again" next to "walls not needed" with no wall buttons.
    expect(document.querySelector('[data-part="wall-hold"]')).toBeNull();
  });

  it('shows nothing extra on a row that is not held', async () => {
    rows.current = [{ ...HELD, row_id: 'row-4', wall_hold_reason: null, wall_reason: null }];
    render(<SlotReaderRows packageId="pkg" refresh={0} />);
    await waitFor(() => expect(screen.getByText('Wall layout for this row')).toBeTruthy());
    expect(document.querySelector('[data-part="wall-hold"]')).toBeNull();
  });
});
