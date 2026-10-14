// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DrawingPartsList } from '@/components/measure/DrawingPartsList';
import { FOUND_BY_CODE, decisionLabel, type PartDrawing, type SuggestedPart } from '@/components/measure/drawingPartChoices';

// Synthetic data only (#1244): a countertop code confirmed because two judgments agreed, as
// `GET …/parts` lists it. Nothing here comes from a client drawing.
const automatic: SuggestedPart = {
  proposal_id: 'p-top',
  view_id: 'v-shop',
  page_index: 0,
  position: 1,
  suggested_kind: 'countertop',
  suggested_code: null,
  reason: 'Suggested by code from the countertop line: the countertop of this row, outlined by its printed overall length.',
  added_by_a_person: false,
  from_countertop_line: true,
  kind_named: true,
  picture_pending: false,
  left_end: { x: '0.2', y: '0.4' },
  right_end: { x: '0.6', y: '0.4' },
  has_picture: true,
  picture_gv_marks: 'not_shown',
  has_crop: false,
  decision: {
    decision: 'confirmed',
    kind: 'countertop',
    code: null,
    decided_by: 'code:bala-two-judgments/v1',
    decided_at: '2026-10-14T10:00:00Z',
    by_code: true,
  },
};

const byAPerson: SuggestedPart = {
  ...automatic,
  proposal_id: 'p-person',
  position: 2,
  decision: { ...automatic.decision!, decided_by: 'reviewer', by_code: false },
};

const drawing: PartDrawing = {
  view_id: 'v-shop',
  page_index: 0,
  tag: 'panel-1',
  role: 'shop',
  can_confirm: true,
  why_not: null,
  page_picture: null,
  parts: [automatic, byAPerson],
};

function renderList(onWithdraw = vi.fn()) {
  render(
    <DrawingPartsList
      drawings={[drawing]}
      loadPagePicture={async () => new Blob()}
      saving={null}
      renderPicture={(_drawing, shown) => <img alt={`Picture of ${shown.proposal_id}`} />}
      renderCrop={() => null}
      onConfirm={vi.fn()}
      onWithdraw={onWithdraw}
      onAdd={vi.fn()}
    />,
  );
  return onWithdraw;
}

function card(name: RegExp): HTMLElement {
  const found = screen.getAllByRole('listitem').find((item) => name.test(item.textContent ?? ''));
  if (!found) throw new Error(`no card matching ${name}`);
  return found;
}

describe('Countertops found by code (#1244)', () => {
  it('says code found it because two checks agreed, and offers Undo', () => {
    renderList();

    const found = within(card(/^.*1\. Suggested as a countertop/));
    expect(found.getByText('Found by code: two checks agreed')).toBeTruthy();
    expect(found.getByRole('button', { name: 'Undo' })).toBeTruthy();
    expect(FOUND_BY_CODE).toBe('Found by code: two checks agreed');
  });

  it('undoes it with a person’s withdrawal', async () => {
    const onWithdraw = renderList();

    await userEvent.click(within(card(/1\. Suggested as a countertop/)).getByRole('button', { name: 'Undo' }));

    expect(onWithdraw).toHaveBeenCalledTimes(1);
    expect(onWithdraw).toHaveBeenCalledWith(automatic);
  });

  it('shows neither on a person’s decision', () => {
    renderList();

    const person = within(card(/2\. Suggested as a countertop/));
    expect(person.queryByText('Found by code: two checks agreed')).toBeNull();
    expect(person.queryByRole('button', { name: 'Undo' })).toBeNull();
  });

  it('says in the decision line that code, not a person, confirmed it', () => {
    expect(decisionLabel(automatic)).toBe('Confirmed as a countertop by code, not by a person.');
    expect(decisionLabel(byAPerson)).toBe('Confirmed as a countertop, no code, by reviewer.');
  });
});
