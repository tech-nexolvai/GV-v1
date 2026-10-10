import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';

import { expect, test, type Locator } from '@playwright/test';

/**
 * P10: the V1 acceptance walkthrough through the screens (#1106). LOCAL ONLY.
 *
 * It walks a restored **mid-review copy** (read and checked once, nothing answered) exactly as a
 * reviewer would: Documents → the set → Results → Show on drawing → the "Needs you" queue (walls,
 * held rows "not checkable" with TEST ONLY notes) → run the checks again → decisions carried over →
 * the rest of the queue → bulk "not checkable" for the other checks → sign off → the three files.
 *
 * **The screen is checked against the API** at every step: any difference is a failure. What the
 * readers produced is not fixed (two runs of the same set can differ), so expectations from earlier
 * runs are only *recorded* as differences in `differences.md`, never asserted.
 *
 * **No client values live here.** Everything about the set (the package, the walls to answer, the
 * first-run table to compare with) comes from a local file named by `GV_E2E_EXPECT`.
 *
 * Environment (README, "Acceptance walkthrough"):
 * - `GV_E2E_BASE_URL`: the dev server, pointed at the local API (`VITE_API_TARGET`);
 * - `GV_E2E_API`: that API's `/api/v1`, on 127.0.0.1 or localhost only, with the reader off;
 * - `GV_E2E_PROJECT`, `GV_E2E_EXPECT`, `GV_E2E_OUT` (video, screenshots, downloads, notes);
 * - `GV_E2E_PYTHON`: a Python with `pypdf`, to read the signed PDF's text.
 */

type Exact = { display: string } | null;
interface Item {
  finding_id: string | null;
  row_id: string;
  page_number: number;
  label: string;
  row_location: { polygon: string[][]; page_number: number } | null;
  outcome: string | null;
  needs_decision: boolean;
  printed_overall: Exact;
  expected_total: Exact;
  delta: Exact;
  hold: { code: string; reason: string } | null;
  reviewer_decision: { action: string; carried_over?: boolean } | null;
  architect?: { outcome: string | null; finding_id: string | null; needs_decision: boolean; not_compared_reason: string | null } | null;
  drawn_length_note?: string | null;
}
interface Results {
  items: Item[];
  pages_without_countertop?: { page_number: number; reason: string }[];
  rows_not_checked?: { page_number: number; reason: string }[];
}
interface Readiness {
  can_approve: boolean;
  blocking_findings: number;
  blocking_finding_ids: string[];
  reason: string | null;
}
interface Expect {
  /** Which set this is, for the notes only. */
  set: string;
  package_id: string;
  /** Walls to answer, by page: a layout ("back_only") or "proposal" (confirm what the readers propose). */
  walls: Record<string, string>;
  /** The kit's first-run table for this copy: `[{page_number, outcome, hold}]`. */
  first: { page_number: number; outcome: string | null; hold: { code: string } | null }[];
  /** Recorded only, never asserted: outcomes earlier runs gave after the walls, by page. */
  after?: Record<string, string[]>;
  /** Recorded only: pages earlier runs listed with no countertop. */
  no_countertop?: number[];
}

const WORDS: Record<string, string> = {
  PASS: 'Looks right',
  FAIL: 'Needs correction',
  REVIEW_REQUIRED: 'Needs your decision',
  NOT_FOUND: 'Waiting on a value',
  NO_APPLICABLE_RULE: 'Not applicable',
};
const WALL_WORDS: Record<string, string> = {
  back_left_right: 'Back wall and both ends',
  back_only: 'Back wall only',
  island: 'Island; no wall ends',
};
const NOTE_HELD = 'TEST ONLY: P10 UI walkthrough; not checkable here, the reviewer would decide on the drawing.';
const NOTE_FAIL = 'TEST ONLY: P10 UI walkthrough; FAIL confirmed as found.';
const NOTE_OTHER = 'TEST ONLY: P10 UI walkthrough; other check not checkable on this set.';

function setting(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set: see frontend/main/README.md, "Acceptance walkthrough".`);
  return value;
}

/** What the result cell says for an item, as the results table words it. */
function resultWords(item: Item): string[] {
  const words: string[] = [];
  if (item.outcome && !item.needs_decision && (item.outcome === 'REVIEW_REQUIRED' || item.outcome === 'NOT_FOUND')) words.push('Not checkable');
  else if (item.outcome) words.push(WORDS[item.outcome] ?? item.outcome);
  else words.push('Not checked');
  if (item.needs_decision && item.outcome !== 'REVIEW_REQUIRED') words.push('Needs you');
  return words;
}

function differenceWords(delta: Exact): string {
  if (!delta) return '—';
  const display = delta.display.trim();
  if (display.startsWith('-')) return `−${display.slice(1)}`;
  return display;
}

const needsYou = (i: Item) => i.needs_decision || Boolean(i.architect?.needs_decision);
const hasFail = (i: Item) => i.outcome === 'FAIL' || i.architect?.outcome === 'FAIL';
const allPass = (i: Item) => i.outcome === 'PASS' && (i.architect?.outcome ?? 'PASS') === 'PASS';

/** What a queue item is about: a countertop's width, or whether it matches the architect. */
type ItemKind = 'countertop' | 'architect';
interface Done {
  rowId: string;
  page: number;
  kind: ItemKind;
  what: string;
}

function local(url: string, name: string): void {
  const host = new URL(url).hostname;
  if (host !== '127.0.0.1' && host !== 'localhost') throw new Error(`${name} must be local, not ${host}`);
}

test('V1 acceptance walkthrough through the screens', async ({ page }) => {
  test.setTimeout(40 * 60_000);
  const api = setting('GV_E2E_API').replace(/\/$/, '');
  local(api, 'GV_E2E_API');
  local(setting('GV_E2E_BASE_URL'), 'GV_E2E_BASE_URL');
  const project = setting('GV_E2E_PROJECT');
  const expectations = JSON.parse(readFileSync(setting('GV_E2E_EXPECT'), 'utf8')) as Expect;
  const out = resolve(setting('GV_E2E_OUT'));
  // The walk saves screenshots of client drawings: never inside the repository.
  const repo = resolve(process.cwd(), '..', '..');
  if (out === repo || out.startsWith(repo + sep)) throw new Error(`GV_E2E_OUT must be outside the repository (${repo})`);
  const python = setting('GV_E2E_PYTHON');
  mkdirSync(out, { recursive: true });
  const pkg = expectations.package_id;
  const root = `${api}/projects/${project}/packages/${pkg}`;
  const differences: string[] = [];
  const notes: string[] = [];
  let shot = 0;
  const snap = async (name: string) => page.screenshot({ path: join(out, `${String(++shot).padStart(2, '0')}-${name}.png`), fullPage: false });
  const get = async <T,>(url: string): Promise<T> => {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`GET ${url}: ${response.status}`);
    return (await response.json()) as T;
  };
  const results = () => get<Results>(`${root}/countertop-results`);
  const readiness = () => get<Readiness>(`${root}/approval-readiness`);
  // The app's top bar: the header that holds the sidebar toggle (other panels have headers too).
  const header = page.locator('header').filter({ has: page.getByRole('button', { name: 'Toggle sidebar' }) });

  // The whole table against the API, row by row, and the cards and the donut against the same rows.
  // Every check retries until the screen shows the API's answer, or fails: no fixed waits.
  async function screenMatchesApi(when: string): Promise<Results> {
    const answer = await results();
    const table = page.locator('[data-slot="countertop-table"] table');
    await expect(table).toBeVisible();
    // The one "not compared" reason every row with an architect result shares, or null (#1126).
    // As the screen's architectState: a result is recorded only with both a finding and an outcome.
    const recorded = (i: Item) => Boolean(i.architect?.finding_id) && i.architect?.outcome !== null && i.architect?.outcome !== undefined;
    const withArchitect = answer.items.filter((i) => i.architect && (recorded(i) || i.architect.not_compared_reason));
    const reasons = new Set(withArchitect.map((i) => (recorded(i) ? null : i.architect!.not_compared_reason)));
    const sharedReason = withArchitect.length > 0 && reasons.size === 1 && !reasons.has(null) ? [...reasons][0] : null;
    await expect(table.locator('tbody tr[data-row-id]')).toHaveCount(answer.items.length);
    for (const item of answer.items) {
      const tr = table.locator(`tr[data-row-id="${item.row_id}"]`);
      const cells = tr.locator('td');
      await expect(cells.nth(1), `${when}: page of ${item.label}`).toHaveText(String(item.page_number));
      for (const word of resultWords(item)) await expect(cells.nth(3), `${when}: result of p${item.page_number}`).toContainText(word);
      await expect(cells.nth(4), `${when}: printed on p${item.page_number}`).toHaveText(item.printed_overall?.display ?? '—');
      await expect(cells.nth(5), `${when}: needed on p${item.page_number}`).toHaveText(item.expected_total?.display ?? '—');
      await expect(cells.nth(6), `${when}: difference on p${item.page_number}`).toContainText(differenceWords(item.delta));
      // The drawn-length note (#1107), only where the API sends one.
      if (item.drawn_length_note) await expect(tr.locator('[data-slot="drawn-length-note"]'), `${when}: drawn-length note on p${item.page_number}`).toHaveText(item.drawn_length_note);
      else await expect(tr.locator('[data-slot="drawn-length-note"]')).toHaveCount(0);
      if (item.hold) await expect(tr.locator(`[data-hold="${item.hold.code}"]`), `${when}: hold on p${item.page_number}`).toBeVisible();
      else await expect(tr.locator('[data-hold]')).toHaveCount(0);
      // "Carried over" exactly where the API says so, and never beside a fresh decision.
      if (item.reviewer_decision?.carried_over) await expect(tr.locator('[data-slot="carried-over"]'), `${when}: carried over on p${item.page_number}`).toBeVisible();
      else await expect(tr.locator('[data-slot="carried-over"]'), `${when}: no carried-over label on p${item.page_number}`).toHaveCount(0);
      // "Matches the architect": a line under the row; "not compared" asks for nothing. When every
      // row shares one "not compared" reason it is said once above the table instead (#1126).
      if (item.architect?.not_compared_reason && sharedReason === null) {
        await expect(table.locator(`tr[data-architect-row="${item.row_id}"]`)).toContainText('Not compared');
        await expect(table.locator(`tr[data-architect-row="${item.row_id}"] button`)).toHaveCount(0);
      }
    }
    const notice = page.locator('[data-slot="architect-notice"]');
    if (sharedReason === null) await expect(notice, `${when}: no shared architect notice`).toHaveCount(0);
    else {
      await expect(notice, `${when}: one architect notice`).toHaveCount(1);
      await expect(table.locator('tr[data-architect-row]'), `${when}: no per-row "not compared" lines`).toHaveCount(0);
    }
    const cards = page.locator('[data-slot="kpi-cards"] button');
    const card = (word: string) => cards.filter({ has: page.getByText(word, { exact: true }) }).locator('.num');
    await expect(card('Countertops'), `${when}: countertops card`).toHaveText(String(answer.items.length));
    await expect(card('Needs you'), `${when}: needs-you card`).toHaveText(String(answer.items.filter(needsYou).length));
    await expect(card('FAIL'), `${when}: FAIL card`).toHaveText(String(answer.items.filter(hasFail).length));
    await expect(card('PASS'), `${when}: PASS card`).toHaveText(String(answer.items.filter(allPass).length));
    const legend = page.getByRole('list', { name: 'Countertop outcomes' });
    const slices = { 'Needs your decision': 0, FAIL: 0, PASS: 0, 'Not checkable': 0 };
    for (const i of answer.items) {
      if (hasFail(i)) slices.FAIL += 1;
      else if (allPass(i)) slices.PASS += 1;
      else if (needsYou(i)) slices['Needs your decision'] += 1;
      else slices['Not checkable'] += 1;
    }
    // Each slice's count is in the legend (visible only for "Not checkable"; the rest for screen
    // readers, since the cards show them, #1126).
    for (const [word, count] of Object.entries(slices)) {
      const slice = legend.locator('li').filter({ has: page.getByText(word, { exact: true }) }).locator('span.num').first();
      await expect(slice, `${when}: donut ${word}`).toHaveText(String(count));
    }
    // Pages with no countertop: listed with the AI's reason, nothing to click.
    const pages = answer.pages_without_countertop ?? [];
    const list = page.getByRole('region', { name: 'Pages with no countertop found' });
    if (pages.length === 0) await expect(list).toHaveCount(0);
    else {
      await expect(list.locator('li')).toHaveCount(pages.length);
      for (const p of pages) await expect(list.locator('li').filter({ hasText: `Page ${p.page_number}` })).toContainText(p.reason.slice(0, 40));
      await expect(list.locator('button')).toHaveCount(0);
    }
    // Countertop lines not checked (#1108): listed only when the API sends any, nothing to click.
    const notChecked = answer.rows_not_checked ?? [];
    const lines = page.getByRole('region', { name: 'Countertop lines not checked' });
    if (notChecked.length === 0) await expect(lines).toHaveCount(0);
    else {
      await expect(lines.locator('li')).toHaveCount(notChecked.length);
      for (const r of notChecked) await expect(lines.locator('li').filter({ hasText: `Page ${r.page_number}` }).first()).toContainText(r.reason.slice(0, 40));
      await expect(lines.locator('button')).toHaveCount(0);
    }
    return answer;
  }

  async function waitForChecks() {
    // The header says "Checking…" while the run is going, and leaves it when the results are in.
    await expect(header.getByRole('button', { name: /Checking/ })).toBeVisible({ timeout: 60_000 });
    await expect(header.getByRole('button', { name: /Checking/ })).toHaveCount(0, { timeout: 15 * 60_000 });
    await expect(page.locator('[data-slot="countertop-table"]')).toBeVisible({ timeout: 120_000 });
  }

  async function showOnDrawing(item: Item, what: string) {
    const tr = page.locator(`[data-slot="countertop-table"] table tr[data-row-id="${item.row_id}"]`);
    await tr.scrollIntoViewIfNeeded();
    await tr.getByRole('button', { name: 'Show on drawing' }).click();
    const viewer = page.locator('[data-slot="drawing-viewer"]');
    await expect(viewer.locator('[data-slot="drawing-page"] img')).toBeVisible({ timeout: 60_000 });
    if (item.row_location) {
      // The outline is the stored polygon, in the page's own 0–1 frame: the stored decimal text read
      // as numbers, point for point (the screen draws with ordinary floating-point numbers).
      const outline = viewer.locator(`[data-outline="${item.row_id}"]`);
      await expect(outline).toBeVisible();
      const drawn = ((await outline.getAttribute('points')) ?? '').trim().split(/\s+/).map((pair) => pair.split(',').map(Number));
      expect(drawn, `outline of p${item.page_number}`).toEqual(item.row_location.polygon.map((point) => point.map(Number)));
    } else {
      await expect(viewer.locator('[data-outline]')).toHaveCount(0);
      await expect(viewer.locator('[data-slot="no-outline"]')).toContainText('No line chosen');
    }
    await snap(`drawing-${what}-p${item.page_number}`);
    await page.getByRole('button', { name: 'Close drawing' }).click();
    await expect(viewer).toHaveCount(0);
  }

  /** Record a decision in the open queue item. A FAIL is never dismissed: only confirmed. */
  async function decide(choice: 'confirm' | 'not-checkable', note: string, outcome: string | null) {
    if (choice === 'not-checkable' && outcome === 'FAIL') throw new Error('the walk never dismisses a FAIL');
    const form = page.locator('[data-slot="queue-decision"]');
    await expect(form).toBeVisible();
    await page.keyboard.press(choice === 'confirm' ? '1' : '3');
    const field = form.getByRole('textbox');
    if (await field.count()) await field.first().fill(note);
    await form.getByRole('button', { name: /Record decision/ }).click();
    await expect(form.getByRole('button', { name: /Saving/ })).toHaveCount(0);
  }

  /**
   * Go through the queue once, doing `act` on each open item it answers; the rest are skipped (J).
   * The queue moves on by itself after a save. An item is a countertop's width, or whether the
   * countertop matches the architect ("…: matches the architect?"): `act` is told which.
   */
  async function throughQueue(phase: string, act: (item: Item, panel: Locator, kind: ItemKind) => Promise<string | null>): Promise<Done[]> {
    const done: Done[] = [];
    const answer = await results();
    const byPage = new Map<number, Item[]>();
    for (const i of answer.items) byPage.set(i.page_number, [...(byPage.get(i.page_number) ?? []), i]);
    const review = header.getByRole('button', { name: /^Review/ }).first();
    await expect(review, `the header offers a review (it says: ${await header.innerText()})`).toBeVisible({ timeout: 60_000 });
    await review.click();
    const queue = page.locator('[data-slot="needs-you-queue"]');
    await expect(queue).toBeVisible();
    const seen = new Set<string>();
    for (let step = 0; step < 60; step++) {
      if (await queue.locator('[data-slot="queue-done"]').isVisible()) break;
      const item = queue.locator('[data-slot="queue-item"]');
      if (!(await item.isVisible())) break;
      const title = (await item.locator('h2').innerText()).trim();
      // A countertop says its page in its name ("Countertop · page N", #1155); a check below it.
      const pageText = (await item.locator('section[aria-label="This item"]').innerText()).match(/[Pp]age (\d+)/)?.[1];
      const status = await item.getAttribute('data-status');
      const key = `${title}|${pageText}|${status}`;
      if (seen.has(key)) break; // back at an item already seen: the end of the list
      seen.add(key);
      const kind: ItemKind = title.endsWith(': matches the architect?') ? 'architect' : 'countertop';
      const name = kind === 'architect' ? title.slice(0, -': matches the architect?'.length) : title;
      // What tells two countertops on one page apart, after "Countertop · page N · " (#1155).
      const extra = name.match(/^Countertop · page \d+ · (.+)$/)?.[1];
      const candidates = pageText ? byPage.get(Number(pageText)) ?? [] : [];
      const row = candidates.find((c) => extra !== undefined && (c.label === extra || c.label.match(/^Countertop (row \S+) on page \d+$/i)?.[1] === extra)) ?? (candidates.length === 1 ? candidates[0] : undefined);
      if (!row && pageText) differences.push(`${phase}: queue item "${title}" on page ${pageText} is not in the results`);
      const did = row && status === 'open' ? await act(row, item, kind) : null;
      if (did) {
        done.push({ rowId: row!.row_id, page: row!.page_number, kind, what: did });
        await snap(`${phase}-p${row!.page_number}${kind === 'architect' ? '-architect' : ''}`);
      } else {
        await page.keyboard.press('j');
      }
      await expect(item).toBeVisible().catch(() => undefined);
    }
    await page.getByRole('button', { name: 'Close the queue' }).click();
    await expect(queue).toHaveCount(0);
    notes.push(`${phase}: ${done.length ? done.map((d) => `p${d.page}${d.kind === 'architect' ? ' (architect)' : ''}: ${d.what}`).join('; ') : 'nothing to do'}`);
    return done;
  }

  /** A "Matches the architect?" item: a recorded FAIL is confirmed, anything else not checkable; a pairing to make is left for a person. */
  async function decideArchitect(row: Item, panel: Locator): Promise<string | null> {
    const architect = row.architect;
    if (!architect?.finding_id || !(await page.locator('[data-slot="queue-decision"]').isVisible())) {
      differences.push(`p${row.page_number}: the architect item asks for a pairing; the walk leaves that to a person`);
      void panel;
      return null;
    }
    if (architect.outcome === 'FAIL') {
      await decide('confirm', NOTE_FAIL, 'FAIL');
      return 'architect FAIL confirmed';
    }
    await decide('not-checkable', NOTE_HELD, architect.outcome);
    return `architect not checkable (${architect.outcome})`;
  }

  try {
    // 1. Documents, then the set --------------------------------------------------------------------
    await test.step('open Documents, then the set', async () => {
      const vendor = (await get<{ vendor: string | null }>(`${api}/projects/${project}/packages/${pkg}`)).vendor ?? '';
      await page.goto('/');
      await page.getByRole('button', { name: 'Documents' }).or(page.getByRole('link', { name: 'Documents' })).first().click();
      await page.getByRole('button', { name: `Open review for ${vendor}` }).first().click();
      await expect(page).toHaveURL(new RegExp(`/review/${pkg}`));
      await expect(page.locator('[data-slot="countertop-table"]')).toBeVisible({ timeout: 60_000 });
      const all = page.getByRole('radio', { name: /^All/ });
      if (await all.count()) await all.first().click();
      await snap('results-first');
    });

    // 2. Results match the API and the copy's first-run table -------------------------------------
    const first = await test.step('Results match the API, page by page', async () => {
      const answer = await screenMatchesApi('first run');
      const firstTable = expectations.first.map((r) => `${r.page_number}:${r.outcome}:${r.hold?.code ?? ''}`).sort();
      const now = answer.items.map((r) => `${r.page_number}:${r.outcome}:${r.hold?.code ?? ''}`).sort();
      expect(now, 'the copy still has its first-run results').toEqual(firstTable);
      for (const i of answer.items) if (i.outcome === 'PASS') differences.push(`p${i.page_number}: automatic PASS before any reviewer answer`);
      const listed = (answer.pages_without_countertop ?? []).map((p) => p.page_number).sort((a, b) => a - b);
      if (expectations.no_countertop && JSON.stringify(listed) !== JSON.stringify(expectations.no_countertop)) differences.push(`no-countertop pages ${JSON.stringify(listed)}, earlier runs ${JSON.stringify(expectations.no_countertop)}`);
      return answer;
    });

    // 3. Show on drawing: a compared row, a held row, a split page -------------------------------
    await test.step('Show on drawing', async () => {
      const compared = first.items.find((i) => i.row_location && !i.hold);
      const held = first.items.find((i) => i.row_location && i.hold && i.hold.code !== 'row-choice-split');
      const split = first.items.find((i) => i.hold?.code === 'row-choice-split');
      if (compared) await showOnDrawing(compared, 'compared');
      else notes.push('no compared row with a stored location to show');
      if (held) await showOnDrawing(held, 'held');
      if (split) await showOnDrawing(split, 'split');
      else notes.push('no split page in this copy');
    });

    // 4. The queue, before any re-run: walls where the plan answers them; held rows not checkable --
    const before = await test.step('queue: walls, and held rows "not checkable"', async () =>
      throughQueue('queue-1', async (row, item, kind) => {
        if (kind === 'architect') return null; // decided after the run, on its new result
        const walls = item.locator('[data-slot="queue-walls"]');
        const plan = expectations.walls[String(row.page_number)];
        // The wall question appears once the queue has the countertop's own row (a moment after the item).
        if (plan) await walls.waitFor({ state: 'visible', timeout: 10_000 }).catch(() => undefined);
        if (plan && (await walls.isVisible())) {
          let wall = plan;
          if (plan === 'proposal') {
            const proposed = (await walls.innerText()).match(/The readers propose: ([^.]+)\./)?.[1]?.trim().toLowerCase();
            wall = Object.entries(WALL_WORDS).find(([, words]) => words.toLowerCase() === proposed)?.[0] ?? '';
            if (!wall) {
              differences.push(`p${row.page_number}: expected a wall proposal to confirm, none`);
              return null;
            }
          }
          const panels = walls.getByRole('button', { name: 'Back only: stone between panels' });
          if (wall === 'back_only' && (await panels.count())) await panels.click();
          else {
            await walls.getByRole('radio', { name: WALL_WORDS[wall] }).click();
            await walls.getByRole('button', { name: 'Use this wall layout' }).click();
          }
          await expect(item).toHaveAttribute('data-status', 'waiting-for-run');
          return `walls ${wall}`;
        }
        if (plan) differences.push(`p${row.page_number}: no wall question offered`);
        if (row.hold && row.finding_id && !plan && row.outcome !== 'FAIL') {
          await decide('not-checkable', NOTE_HELD, row.outcome);
          return `not checkable (${row.hold.code})`;
        }
        return null;
      }),
    );

    // 5. Run the checks when the screen says so; decisions on unchanged results carry over -------
    await test.step('run the checks again; unchanged decisions carry over', async () => {
      const walled = before.filter((d) => d.what.startsWith('walls'));
      const dismissed = before.filter((d) => d.what.startsWith('not checkable'));
      const run = header.getByRole('button', { name: 'Run checks' });
      // A saved wall answer only counts after a run, so the screen must ask for one.
      if (walled.length) await expect(run, 'the header asks for a check run').toBeVisible({ timeout: 30_000 });
      if (!(await run.isVisible())) {
        notes.push('the screen asked for no check run (no wall answered), so carry-over was not exercised on this set');
        return;
      }
      const old = await results();
      // The header takes the reviewer to Measurements; the run starts from its own "Run checks".
      await run.click();
      await page.locator('#measure-run-checks').click();
      await waitForChecks();
      await page.getByRole('tab', { name: /Results/ }).click();
      const answer = await screenMatchesApi('after the re-run');
      // The run made new results: every countertop's result is a new record.
      const oldIds = new Map(old.items.map((i) => [i.row_id, i.finding_id]));
      const same = answer.items.filter((i) => i.finding_id !== null && oldIds.get(i.row_id) === i.finding_id);
      expect(same.map((i) => `p${i.page_number}`), 'results the run did not replace').toEqual([]);
      // Every "not checkable" made before the run carries over when its result came back the same,
      // and says so; a row answered with walls has no carried decision.
      const was = new Map(old.items.map((i) => [i.row_id, `${i.outcome}:${i.hold?.code ?? ''}`]));
      for (const d of dismissed) {
        const now = answer.items.find((i) => i.row_id === d.rowId);
        if (!now) {
          differences.push(`p${d.page}: not in the results after the run`);
          continue;
        }
        if (`${now.outcome}:${now.hold?.code ?? ''}` !== was.get(d.rowId)) {
          differences.push(`p${d.page}: its result changed in the run (${was.get(d.rowId)} → ${now.outcome}:${now.hold?.code ?? ''}), so its decision is asked again`);
          continue;
        }
        expect(now.reviewer_decision?.carried_over, `p${d.page}: the decision made before the run carries over`).toBe(true);
        await expect(page.locator(`[data-slot="countertop-table"] table tr[data-row-id="${d.rowId}"] [data-slot="carried-over"]`)).toBeVisible();
      }
      for (const d of walled) {
        const now = answer.items.find((i) => i.row_id === d.rowId);
        expect(now?.reviewer_decision?.carried_over ?? false, `p${d.page}: a wall answer is not a carried decision`).toBe(false);
      }
      notes.push(`carried over after the re-run: ${answer.items.filter((i) => i.reviewer_decision?.carried_over).map((i) => `p${i.page_number}`).join(', ') || 'none'} (not checkable before it: ${dismissed.length})`);
      await snap('results-after-rerun');
    });

    // 6. The rest of the queue: FAIL confirmed, anything still held not checkable ---------------
    await test.step('queue: the rest', async () => {
      await throughQueue('queue-2', async (row, panel, kind) => {
        if (kind === 'architect') return decideArchitect(row, panel);
        if (!row.finding_id) return null;
        if (row.outcome === 'FAIL') {
          await decide('confirm', NOTE_FAIL, 'FAIL');
          return 'FAIL confirmed';
        }
        await decide('not-checkable', NOTE_HELD, row.outcome);
        return `not checkable (${row.hold?.code ?? row.outcome})`;
      });
      // Anything the countertop queue does not hold (the other checks) in one go, with one note.
      const others = page.getByRole('region', { name: 'Other checks' });
      const bulk = others.getByRole('button', { name: /Mark not checkable/ });
      if (!(await bulk.isVisible())) await others.getByRole('button', { name: /Other checks/ }).click();
      if (await bulk.count()) {
        await bulk.click();
        const dialog = page.getByRole('dialog', { name: /not checkable/ });
        await dialog.getByLabel(/Why are these not checkable/).fill(NOTE_OTHER);
        await dialog.getByRole('button', { name: /^Mark \d+ not checkable/ }).click();
        await expect(dialog).toHaveCount(0, { timeout: 120_000 });
      }
      const ready = await readiness();
      expect(ready.blocking_findings, `still blocking: ${ready.reason ?? ''}`).toBe(0);
      expect(ready.can_approve, ready.reason ?? '').toBe(true);
      await page.getByRole('button', { name: 'Refresh results' }).click();
      const final = await screenMatchesApi('before sign-off');
      for (const [p, allowed] of Object.entries(expectations.after ?? {})) {
        const i = final.items.find((x) => String(x.page_number) === p);
        if (!i) differences.push(`p${p}: not in the results`);
        else if (!allowed.includes(i.outcome ?? 'none')) differences.push(`p${p}: ${i.outcome} (printed ${i.printed_overall?.display ?? '—'}, needed ${i.expected_total?.display ?? '—'}), earlier runs ${allowed.join(' or ')}`);
      }
      await snap('results-ready');
    });

    // 7. Sign off through the confirmation, then the three files ---------------------------------
    await test.step('sign off, then download the signed files', async () => {
      await header.getByRole('button', { name: 'Sign off' }).click();
      const dialog = page.getByRole('dialog', { name: 'Sign off this review?' });
      await snap('signoff-confirmation');
      await dialog.getByRole('button', { name: 'Sign off' }).click();
      const report = page.getByRole('region', { name: 'Signed report' });
      await expect(report).toBeVisible({ timeout: 120_000 });
      const pdf = report.getByRole('button', { name: 'Download PDF' });
      for (let i = 0; i < 60 && !(await pdf.isVisible()); i++) {
        await page.waitForTimeout(5000);
        const again = report.getByRole('button', { name: 'Check again' });
        if (await again.count()) await again.click();
      }
      await expect(pdf).toBeVisible();
      await snap('signed-report');
      for (const [button, file] of [['Download PDF', 'report.pdf'], ['Download workbook', 'report.xlsx'], ['Download redline', 'redline.pdf']] as const) {
        const download = page.waitForEvent('download');
        await report.getByRole('button', { name: button }).click();
        await (await download).saveAs(join(out, file));
      }
    });

    // The PDF lists every countertop, its outcome, the TEST ONLY notes and the no-countertop pages.
    await test.step('the signed PDF says it all', async () => {
      const text = execFileSync(python, ['-I', '-c', 'import sys, pypdf; print("\\n".join(p.extract_text() or "" for p in pypdf.PdfReader(sys.argv[1]).pages))', join(out, 'report.pdf')], { encoding: 'utf8' });
      writeFileSync(join(out, 'report.txt'), text);
      const flat = text.replace(/\s+/g, ' ');
      expect(flat).toContain('TEST ONLY');
      const final = await results();
      for (const i of final.items) expect(flat, `the PDF names ${i.label}`).toContain(i.label);
      for (const p of final.pages_without_countertop ?? []) expect(flat, `the PDF lists page ${p.page_number} with no countertop`).toMatch(new RegExp(`(?:Page|p\\.?)\\s*${p.page_number}\\b`, 'i'));
    });
  } finally {
    // Written whatever happened, so a failed walk still says what it did and what differed.
    writeFileSync(join(out, 'differences.md'), [
      `# P10 UI walkthrough: ${expectations.set} (package ${pkg})`,
      '',
      '## Differences from earlier runs (recorded, not failures)',
      ...(differences.length ? differences.map((d) => `- ${d}`) : ['- none']),
      '',
      '## What was done',
      ...notes.map((n) => `- ${n}`),
      '',
    ].join('\n'));
  }
});
