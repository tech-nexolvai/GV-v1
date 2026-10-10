// @vitest-environment jsdom
/**
 * The last small screen fixes from the UI audit (#1155). Synthetic data only: nothing here comes
 * from a client drawing.
 */
import fs from 'node:fs';
import path from 'node:path';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ArchitectResult, CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { distinguishingLabel } from '@/lib/countertop-results';
import { CountertopTable, type RowActions } from '@/components/results/countertop-table';
import { NeedsYouQueue, type QueueProps } from '@/components/queue/needs-you-queue';
import { AppTopbar } from '@/components/shell/app-topbar';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { SidebarProvider } from '@/components/ui/sidebar';

const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Countertop row on page ${page}`,
    row_location: null, outcome: 'REVIEW_REQUIRED', needs_decision: true,
    printed_overall: x('40', '40"'),
    pieces: [{ index: 0, value: x('20', '20"'), kind: 'cabinet', source: 'sealed' }, { index: 1, value: x('20', '20"'), kind: 'cabinet', source: 'sealed' }],
    field_cut_per_end: x('1', '1"'), field_cut_count: 0, expected_total: null, delta: null,
    hold: null, reviewer_decision: null,
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [true, true] },
    ...overrides,
  };
}

const ACTIONS: RowActions = { onShowDrawing: () => {}, onOpenCard: () => {}, onDecide: () => {}, canDecide: (r) => r.needs_decision };
const phoneCards = () => within(screen.getByRole('list', { name: 'Countertops' })).getAllByRole('listitem');
const tableRow = (id: string) => document.querySelector<HTMLElement>(`[data-slot="countertop-table"] tr[data-row-id="${id}"]`)!;

// ── 1. The page once ─────────────────────────────────────────────

describe('a countertop says its page once (#1155)', () => {
  it('names the page once on the phone card, and the table does not repeat its Page column', () => {
    render(<CountertopTable rows={[row('a', 2)]} actions={ACTIONS} />);
    const [card] = phoneCards();
    expect(card.querySelector('p')?.textContent).toBe('Countertop · page 2');
    expect(card.textContent).not.toContain('Countertop row on page 2');
    expect(card.textContent).not.toMatch(/Page\s*2/); // no second "Page 2" line under the title
    // The table: the page in its own column, the name without it.
    const cells = tableRow('a').querySelectorAll('td');
    expect(cells[1].textContent).toBe('2');
    expect(cells[2].querySelector('span')?.textContent).toBe('Countertop');
    expect(cells[2].querySelector('span')?.getAttribute('title')).toBe('Countertop row on page 2'); // the API's whole label stays reachable
  });

  it('keeps the label when it tells two countertops on the same page apart, even one the filter hides', () => {
    const one = row('one', 3, { label: 'Countertop row 3.1 on page 3' });
    const two = row('two', 3, { label: 'Countertop row 3.2 on page 3' });
    const alone = row('alone', 4, { label: 'Countertop row 4.1 on page 4' });
    render(<CountertopTable rows={[one, alone]} allRows={[one, two, alone]} actions={ACTIONS} />);
    expect(phoneCards().map((card) => card.querySelector('p')?.textContent)).toEqual(['Countertop · page 3 · row 3.1', 'Countertop · page 4']);
    // Numbers in the number face, words in the word face.
    expect([...phoneCards()[0].querySelectorAll('p .num')].map((n) => n.textContent)).toEqual(['3', '3.1']);
    expect(tableRow('one').querySelectorAll('td')[2].querySelector('span')?.textContent).toBe('Countertop · row 3.1');
    expect(tableRow('alone').querySelectorAll('td')[2].querySelector('span')?.textContent).toBe('Countertop');
  });

  it('keeps any other label whole, and never a generic one', () => {
    const rows = [row('a', 5, { label: 'Synthetic island' }), row('b', 5)];
    expect(distinguishingLabel(rows[0], rows)).toBe('Synthetic island');
    expect(distinguishingLabel(rows[1], rows)).toBeNull();
  });
});

// ── 6. "Not decided yet" ─────────────────────────────────────────

describe('"Decided by" says it in words (#1155)', () => {
  it('a countertop still waiting reads "Not decided yet", muted, never "Pending"', () => {
    render(<CountertopTable rows={[row('a', 2)]} actions={ACTIONS} />);
    const cell = tableRow('a').querySelectorAll('td')[8];
    expect(cell.textContent).toBe('Not decided yet');
    expect(cell.querySelector('span')?.className).toContain('text-muted-foreground');
    expect(document.body.textContent).not.toContain('Pending');
  });
});

// ── The queue: its header on a touch screen (4) and its Architect line (7) ───

const REASON = 'Not compared: the synthetic architect drawing prints only centre lines.';
const notCompared = (reason: string): ArchitectResult => ({ outcome: null, finding_id: null, reason: null, needs_decision: false, compared: [], not_compared_reason: reason, pairing_source: null, pairing_judgments: null });
const finding = (id: string): Finding => ({ id, check_id: `RULE-${id}`, name: `rule ${id}`, outcome: 'REVIEW_REQUIRED', severity: 'MAJOR', reviewer_action: null, scope_label: null }) as Finding;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

function queue(rows: CountertopResult[]) {
  const props: QueueProps = {
    open: true, opening: 1, onOpenChange: vi.fn(),
    rows, rowsReady: true, findings: rows.map((r) => finding(r.finding_id!)), blocking: new Set(rows.map((r) => r.finding_id!)),
    projectId: 'p', packageId: 'k',
    handlers: { onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) },
    next: { kind: 'sign-off', label: 'Sign off', disabled: false, reason: null }, onAct: vi.fn(), onWallSaved: vi.fn(), onOpenCard: vi.fn(),
  };
  return render(<NeedsYouQueue {...props} />);
}
const item = () => document.querySelector<HTMLElement>('[data-slot="queue-item"]')!;

describe('the queue (#1155)', () => {
  const realMatchMedia = window.matchMedia;
  let slotRows: unknown[] = [];
  beforeEach(() => {
    slotRows = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/slot-rows')) return json({ rows: slotRows });
      if (url.endsWith('/rules')) return json([]);
      return json({ error: 'http_error', message: 'not ready', request_id: 'r' }, 404);
    }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    window.matchMedia = realMatchMedia;
  });

  it('names a countertop item once: "Countertop · page N", with no second "Page N" beside it', () => {
    queue([row('a', 2)]);
    expect(item().querySelector('h2')?.textContent).toBe('Countertop · page 2');
    expect(item().querySelector('section[aria-label="This item"]')?.textContent).not.toMatch(/Page\s*2/);
  });

  it('says a shared "not compared" reason in one short line, the reason behind "Why?"', async () => {
    const user = userEvent.setup();
    queue([row('a', 2, { architect: notCompared(REASON) }), row('b', 3, { architect: notCompared(REASON) })]);
    const line = item().querySelector<HTMLElement>('[data-slot="architect-line"]')!;
    expect(line.getAttribute('data-shared')).toBe('true');
    expect(line.querySelector('[data-architect="not-compared"]')?.textContent).toBe('Not compared on any countertop');
    const reason = line.querySelector<HTMLElement>('[data-slot="architect-shared-reason"]')!;
    expect(reason.hidden).toBe(true);
    await user.click(within(line).getByRole('button', { name: 'Why?' }));
    expect(reason.hidden).toBe(false);
    expect(reason.textContent).toBe('The synthetic architect drawing prints only centre lines.');
  });

  it('lays the wall choices out in one column or all in one row, never 2 + 1, and starts their reason with a capital', async () => {
    slotRows = [{
      row_id: 'a', page_number: 2, label: 'Countertop row on page 2', piece_count: 2, held_reason: null,
      wall_confirmation_allowed: true, values: [], wall_proposal: null, wall_source: null, wall_reason: 'the synthetic readers do not agree',
      wall_layout_choices: ['back_left_right', 'back_only', 'island'], decision_id: null, confirmed_by: null, decided_at: null, wall_config: null,
    }];
    queue([row('a', 2)]);
    await waitFor(() => expect(item().querySelector('[data-slot="queue-walls"]')).not.toBeNull());
    const walls = item().querySelector<HTMLElement>('[data-slot="queue-walls"]')!;
    expect(walls.className).toContain('@container');
    const group = walls.querySelector<HTMLElement>('[data-slot="toggle-group"]')!;
    const classes = group.className.split(/\s+/);
    // One column unless the box is wide enough for all three side by side; no auto-fit that can wrap 2 + 1.
    expect(classes).toContain('grid-cols-1');
    expect(classes).toContain('@min-[42rem]:grid-flow-col');
    expect(group.className).not.toMatch(/auto-fit|auto-fill|sm:grid-cols-2/);
    expect(within(walls).getByText('The synthetic readers do not agree')).toBeTruthy();
  });

  it('keeps the whole sentence when the countertops do not share one reason', () => {
    queue([row('a', 2, { architect: notCompared(REASON) }), row('b', 3, { architect: notCompared('Not compared: a different synthetic reason.') })]);
    const line = item().querySelector<HTMLElement>('[data-slot="architect-line"]')!;
    expect(line.getAttribute('data-shared')).toBeNull();
    expect(line.textContent).toContain(REASON);
  });

  it('shows the J / K keys where there is a keyboard, and leaves them out only on a touch-only screen', () => {
    const header = () => document.querySelector<HTMLElement>('[data-slot="queue-header"]')!;
    // A screen as the browser describes it: which pointers it has.
    const pointers = ({ primaryCoarse, anyFine }: { primaryCoarse: boolean; anyFine: boolean }) => {
      window.matchMedia = ((query: string) => ({
        matches: query === '(pointer: coarse)' ? primaryCoarse : query === '(any-pointer: fine)' ? anyFine : query === 'not all and (any-pointer: fine)' ? !anyFine : false,
        media: query, onchange: null,
        addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
      }) as MediaQueryList) as typeof window.matchMedia;
    };
    // A desktop, and a tablet whose touch screen comes with a trackpad and keyboard: the keys show.
    for (const screenKind of [{ primaryCoarse: false, anyFine: true }, { primaryCoarse: true, anyFine: true }]) {
      pointers(screenKind);
      const { unmount } = queue([row('a', 2), row('b', 3)]);
      expect([...header().querySelectorAll('kbd')].map((k) => k.textContent)).toEqual(['K', 'J']);
      expect(screen.getByRole('button', { name: 'Next item (J)' })).toBeTruthy();
      unmount();
    }

    // Touch only: no keys to press, so no hints.
    pointers({ primaryCoarse: true, anyFine: false });
    queue([row('a', 2), row('b', 3)]);
    expect(header().querySelectorAll('kbd')).toHaveLength(0);
    // Position and close stay; moving is by the arrows.
    expect(header().querySelector('[aria-live="polite"]')?.textContent).toBe('1 / 2');
    expect(screen.getByRole('button', { name: 'Close the queue' })).toBeTruthy();
    act(() => void fireEvent.click(screen.getByRole('button', { name: 'Next item' })));
    expect(item().querySelector('h2')?.textContent).toBe('Countertop · page 3');
    expect(document.querySelector('[data-slot="queue-decision"]')?.textContent).not.toContain('J K move');
  });
});

// ── 3. The breadcrumb ────────────────────────────────────────────

describe('the top bar (#1155)', () => {
  it('lets "Documents" give way to the review\'s name when the left half is short of room', () => {
    render(
      <SidebarProvider>
        <AppTopbar title="Synthetic set" crumbs={[{ label: 'Documents', onSelect: () => {} }, { label: 'Synthetic set' }, { label: 'Revision 1' }]} titleRef={() => {}} actionsRef={() => {}} />
      </SidebarProvider>,
    );
    const where = document.querySelector('[data-slot="topbar-where"]')!;
    expect(where.className).toContain('@container');
    const [documents, name, revision] = [...where.querySelectorAll('[data-slot="breadcrumb-item"]')];
    // "Documents" (and its separator) only with room to spare; the name takes the room otherwise.
    expect(documents.className).toContain('@min-[44rem]:inline-flex');
    expect(documents.className).not.toContain('md:inline-flex');
    expect(documents.nextElementSibling?.className).toContain('@min-[44rem]:inline-flex');
    expect(name.className).toContain('min-w-0');
    expect(revision.className).toContain('shrink-0');
  });
});

// ── 9. Popovers draw their border with the border token ────────────

describe('the shared popover (#1155)', () => {
  it('draws its edge in the border colour, not the text colour', async () => {
    const user = userEvent.setup();
    render(
      <Popover>
        <PopoverTrigger>Open</PopoverTrigger>
        <PopoverContent>Synthetic</PopoverContent>
      </Popover>,
    );
    await user.click(screen.getByRole('button', { name: 'Open' }));
    const content = await waitFor(() => document.querySelector<HTMLElement>('[data-slot="popover-content"]')!);
    expect(content.className.split(/\s+/)).toContain('border-border');
  });

  it('so do the other pop-up surfaces that draw an edge', () => {
    const UI = path.resolve(__dirname, '../../src/components/ui');
    for (const [file, slot] of [['hover-card.tsx', 'hover-card-content'], ['dropdown-menu.tsx', 'dropdown-menu-content'], ['dialog.tsx', 'dialog-content'], ['select.tsx', 'select-content']]) {
      const source = fs.readFileSync(path.join(UI, file), 'utf8');
      const block = source.slice(source.indexOf(`data-slot="${slot}"`));
      expect(block.slice(0, 800), file).toContain('border-border'); // its own class list, just after the slot
    }
  });
});

// ── 2. Nothing under 12px ───────────────────────────────────────

describe('nothing under 12px (#1155)', () => {
  const SRC = path.resolve(__dirname, '../../src');
  const files = (dir: string): string[] =>
    fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      return entry.isDirectory() ? files(full) : /\.(css|tsx?)$/.test(entry.name) ? [full] : [];
    });

  it('no stylesheet, inline size or Tailwind arbitrary size sets text under 12px', () => {
    const small: string[] = [];
    for (const file of files(SRC)) {
      const text = fs.readFileSync(file, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
      const where = path.relative(SRC, file);
      for (const m of text.matchAll(/font-size:\s*([\d.]+)px/g)) if (Number(m[1]) < 12) small.push(`${where}: ${m[0]}`);
      for (const m of text.matchAll(/font-size:\s*([\d.]+)rem/g)) if (Number(m[1]) * 16 < 12) small.push(`${where}: ${m[0]}`);
      for (const m of text.matchAll(/fontSize=\{([\d.]+)\}/g)) if (Number(m[1]) < 12) small.push(`${where}: ${m[0]}`);
      for (const m of text.matchAll(/text-\[([\d.]+)px\]/g)) if (Number(m[1]) < 12) small.push(`${where}: ${m[0]}`);
    }
    expect(small).toEqual([]);
  });
});
