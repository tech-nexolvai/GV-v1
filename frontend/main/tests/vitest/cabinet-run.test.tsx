// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ItemDetail } from '@/components/simple-review/item-detail';
import { ItemRow } from '@/components/simple-review/item-row';
import { reviewItems } from './fixtures/review-items';

// Synthetic data only: nothing here comes from a client drawing.
const data = reviewItems();
const base = data.items.find((entry) => entry.key === 'countertop:find-5')!;

const width = (display: string, numerator: string) => ({ numerator, denominator: '1', display });

const runItem = (overrides: Record<string, unknown> = {}) => ({
  ...base,
  key: 'cabinet-run:find-cab',
  kind: 'cabinet_run' as const,
  what: 'Which cabinets keep their width?',
  status: 'open' as const,
  actions: ['use_cabinet_run'],
  finding_id: 'find-cab',
  page: 4,
  best_guess: null,
  vendor_ref: null,
  cabinet_choices: [
    { category: 'single_door', label: 'Single door', keeps_width: false },
    { category: 'drawer', label: 'Drawer', keeps_width: false },
    { category: 'sink_cabinet', label: 'Sink cabinet', keeps_width: true },
  ],
  settings: [],
  cabinet_proposal: {
    runs: [
      {
        row_id: 'row-1',
        page: 4,
        pieces: [
          { position: 1, width: width('2"', '2'), printed: '2" Filler', candidate_id: 'c1', printed_kind: 'filler', proposed: 'filler' },
          { position: 2, width: width('24"', '24'), printed: '24"', candidate_id: 'c2', printed_kind: null, proposed: null },
          { position: 3, width: width('30"', '30'), printed: '30"', candidate_id: 'c3', printed_kind: null, proposed: null },
          { position: 4, width: width('2"', '2'), printed: '2"', candidate_id: 'c4', printed_kind: null, proposed: null },
        ],
        total: width('58"', '58'),
        checks: ['Each of the 4 widths was read alike by two AIs, in the vendor\'s ink.'],
        vendor_ref: null,
        architect_note: "The architect's drawings give no cabinet widths for this run.",
      },
    ],
    not_proposed: [{ page: 6, row_id: 'row-2', why: 'Piece 2: the two AIs did not read the same inches.' }],
    note: null,
    site_width: null,
    ...overrides,
  },
});

describe('CabinetRun (cabinets e2e)', () => {
  it('shows the widths the AIs read, pre-selects only a printed filler, and uses the run once', async () => {
    const onAct = vi.fn();
    render(<ItemDetail item={runItem()} busy={null} error={null} onAct={onAct} onOpenPdfs={() => {}} />);
    const use = screen.getByRole('button', { name: /Use this run/ }) as HTMLButtonElement;
    expect(use.disabled).toBe(true);
    // No agree or disagree button: the run's answers are the answer.
    expect(screen.queryByRole('button', { name: /It is right/ })).toBeNull();
    // Only the piece the vendor printed "Filler" on comes pre-selected.
    expect((screen.getByRole('combobox', { name: 'Piece 1: what it is' }) as HTMLSelectElement).value).toBe('filler');
    expect((screen.getByRole('combobox', { name: 'Piece 4: what it is' }) as HTMLSelectElement).value).toBe('');

    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 2: what it is' }), 'single_door');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 3: what it is' }), 'sink_cabinet');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 4: what it is' }), 'filler');
    expect(use.disabled).toBe(true);
    expect(screen.getByText('Enter the site wall-to-wall.')).toBeTruthy();

    await userEvent.type(screen.getByRole('textbox', { name: 'Site wall-to-wall in inches' }), '60 1/2"');
    expect(use.disabled).toBe(false);
    await userEvent.click(use);
    expect(onAct).toHaveBeenCalledWith('use_cabinet_run', {
      cabinet_run: { row_id: 'row-1', pieces: ['filler', 'single_door', 'sink_cabinet', 'filler'], site_width: '60 1/2"' },
    });
  });

  it('refuses a run without one filler at each end, saying to check it by hand', async () => {
    render(<ItemDetail item={runItem()} busy={null} error={null} onAct={() => {}} onOpenPdfs={() => {}} />);
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 2: what it is' }), 'filler');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 3: what it is' }), 'drawer');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 4: what it is' }), 'drawer');
    expect(screen.getByText(/one filler at each end and cabinets between/)).toBeTruthy();
    expect((screen.getByRole('button', { name: /Use this run/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps a saved site width and lists the rows not proposed with their reason', async () => {
    const onAct = vi.fn();
    render(<ItemDetail item={runItem({ site_width: width('60"', '60') })} busy={null} error={null} onAct={onAct} onOpenPdfs={() => {}} />);
    expect(screen.getByText(/Leave empty to keep it/)).toBeTruthy();
    const details = screen.getByText(/Rows read but not proposed/).closest('details')!;
    expect(within(details).getByText(/did not read the same inches/)).toBeTruthy();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 2: what it is' }), 'drawer');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 3: what it is' }), 'drawer');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Piece 4: what it is' }), 'filler');
    await userEvent.click(screen.getByRole('button', { name: /Use this run/ }));
    expect(onAct).toHaveBeenCalledWith('use_cabinet_run', {
      cabinet_run: { row_id: 'row-1', pieces: ['filler', 'drawer', 'drawer', 'filler'], site_width: null },
    });
  });

  it('says why nothing is proposed, and the closed row says to enter it by hand', () => {
    const empty = runItem({ runs: [], not_proposed: [], note: 'No cabinet widths were read yet.' });
    render(<ItemDetail item={empty} busy={null} error={null} onAct={() => {}} onOpenPdfs={() => {}} />);
    expect(screen.getByText('No cabinet widths were read yet.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Use this run/ })).toBeNull();
    render(<ItemRow item={empty} open={false} active={false} onToggle={() => {}} />);
    expect(screen.getByText('Enter by hand')).toBeTruthy();
  });
});
