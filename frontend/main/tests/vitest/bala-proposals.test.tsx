// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DrawingPartsList } from '@/components/measure/DrawingPartsList';
import type { PartDrawing, SuggestedPart } from '@/components/measure/drawingPartChoices';

// Synthetic data only (#1236): Bala's suggestions from one countertop row, as `GET …/parts` lists
// them. Nothing here comes from a client drawing.
const part = (overrides: Partial<SuggestedPart>): SuggestedPart => ({
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
  decision: null,
  ...overrides,
});

const drawing = (parts: SuggestedPart[], canConfirm = true): PartDrawing => ({
  view_id: 'v-shop',
  page_index: 0,
  tag: 'panel-1',
  role: canConfirm ? 'shop' : null,
  can_confirm: canConfirm,
  why_not: canConfirm ? null : 'Confirm whose drawing this is first.',
  page_picture: null,
  parts,
});

const countertop = part({});
const filler = part({
  proposal_id: 'p-filler',
  position: 2,
  suggested_kind: 'filler',
  reason: 'Suggested by code from the countertop line: piece 1 of 2 of this row, suggested as a filler because the drawing names it (printed word "FILLER").',
});
const fromCode = part({
  proposal_id: 'p-code',
  position: 3,
  suggested_kind: 'cabinet',
  from_countertop_line: false,
  reason: 'One of 2 dimensions drawn end to end along the lowest row.',
});

function renderList(parts: SuggestedPart[], canConfirm = true, onWithdraw = vi.fn()) {
  const onConfirm = vi.fn();
  render(
    <DrawingPartsList
      drawings={[drawing(parts, canConfirm)]}
      loadPagePicture={async () => new Blob()}
      saving={null}
      renderPicture={(_drawing, shown) => <img alt={`Picture of ${shown.proposal_id}`} />}
      renderCrop={() => null}
      onConfirm={onConfirm}
      onWithdraw={onWithdraw}
      onAdd={vi.fn()}
    />,
  );
  return onConfirm;
}

function card(name: RegExp): HTMLElement {
  const found = screen.getAllByRole('listitem').find((item) => name.test(item.textContent ?? ''));
  if (!found) throw new Error(`no card matching ${name}`);
  return found;
}

describe("Bala's suggestions on the Measure page (#1236)", () => {
  it('says each came from the countertop line, with its picture', () => {
    renderList([countertop, filler, fromCode]);

    expect(within(card(/^.*1\. Suggested as a countertop/)).getByText('Suggested by code from the countertop line')).toBeTruthy();
    expect(within(card(/2\. Suggested as a filler/)).getByText('Suggested by code from the countertop line')).toBeTruthy();
    expect(within(card(/3\. Suggested as a cabinet/)).queryByText('Suggested by code from the countertop line')).toBeNull();
    expect(screen.getByAltText('Picture of p-top')).toBeTruthy();
  });

  it('confirms one with a single click, its kind already filled in', async () => {
    const onConfirm = renderList([countertop, filler]);

    await userEvent.click(within(card(/2\. Suggested as a filler/)).getByRole('button', { name: 'Confirm: filler' }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith(filler, 'filler', null);
  });

  it('offers no one-click confirmation once a person has decided', () => {
    renderList([
      part({
        decision: { decision: 'confirmed', kind: 'countertop', code: null, decided_by: 'reviewer', decided_at: '2026-10-14T10:00:00Z', by_code: false },
      }),
    ]);
    expect(screen.queryByRole('button', { name: /^Confirm: / })).toBeNull();
  });

  it('keeps the button but disabled where the drawing is not confirmed as the vendor’s', () => {
    renderList([countertop], false);
    const button = screen.getByRole('button', { name: 'Confirm: countertop' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  // Review of #1243: about 99 pieces in 100 have no printed kind. Then nothing is filled in.
  const unnamed = part({
    proposal_id: 'p-piece',
    position: 4,
    suggested_kind: 'cabinet',
    kind_named: false,
    reason: 'Suggested by code from the countertop line: piece 2 of 2 of this row, suggested as a cabinet because nothing printed names its kind; say what it is.',
  });

  it('offers no one-click confirmation where nothing printed names the kind', () => {
    renderList([unnamed]);
    expect(screen.queryByRole('button', { name: /^Confirm: / })).toBeNull();
    const choices = within(card(/4\. /)).getByRole('group', { name: /What is part 4/ });
    for (const name of ['Cabinet', 'Filler', 'Appliance space (not a part)', 'Something else (not a part)']) {
      expect(within(choices).getByRole('button', { name })).toBeTruthy();
    }
  });

  it('records the person’s own answer: a kind confirms it, an appliance space says it is not a part', async () => {
    const onWithdraw = vi.fn();
    const onConfirm = renderList([unnamed], true, onWithdraw);
    const choices = within(card(/4\. /)).getByRole('group', { name: /What is part 4/ });

    await userEvent.click(within(choices).getByRole('button', { name: 'Filler' }));
    expect(onConfirm).toHaveBeenCalledWith(unnamed, 'filler', null);

    await userEvent.click(within(choices).getByRole('button', { name: 'Appliance space (not a part)' }));
    expect(onWithdraw).toHaveBeenCalledWith(unnamed);
  });

  it('says a picture is being made while the worker cuts it', () => {
    renderList([part({ has_picture: false, picture_pending: true })]);
    expect(screen.getByText('Its picture is being made. Reload in a moment to see it.')).toBeTruthy();
  });
});
