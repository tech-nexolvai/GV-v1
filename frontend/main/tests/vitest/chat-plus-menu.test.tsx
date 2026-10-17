// @vitest-environment jsdom
/**
 * The composer's "+" menu (#1517): open, search and keyboard; each option's prompt or action; the
 * blanks a prompt leaves ("[page number]", "[area]": highlighted, selected, Tab between them, Send
 * waits); Focus setting the product chips and the effort; the $0 files using the existing calls
 * (the markup-pdf route, the CSV export); the full check confirmed first; recently used first.
 * Synthetic data only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatComposer } from '@/components/chat/chat-composer';
import { PlusMenu, type PlusMenuProps } from '@/components/chat/chat-plus-menu';
import { ShellSlotsContext } from '@/components/shell/shellSlots';
import type { ChatApi, ChatFile, ChatMessage, ChatProduct } from '@/lib/chat-api';
import { downloadFile, downloadFindingsCsv } from '@/lib/chat-exports';
import type { ViewMessage } from '@/lib/chat-messages';
import {
  areaPrompt,
  blankRanges,
  blankSegments,
  effortFor,
  firstBlank,
  FULL_CHECK_MINUTES,
  fullCheckEstimate,
  knownAreas,
  lastFindings,
  nextBlank,
  PLUS_ACTIONS,
  plusAction,
  plusActions,
  readRecent,
  rememberRecent,
  sameProducts,
  withPrompt,
} from '@/lib/chat-plus-actions';
import type { ChatChoice } from '@/lib/chat-settings';
import { ChatPage } from '@/pages/ChatPage';
import { chatOut, fakeChatApi, finding, modelsOut, OPUS, QUESTION, SONNET } from './fixtures/chat';

// The two downloads are stubbed (jsdom cannot save files); the plain-words helper stays real.
vi.mock('@/lib/chat-exports', async (original) => ({ ...(await original<typeof import('@/lib/chat-exports')>()), downloadFile: vi.fn(), downloadFindingsCsv: vi.fn() }));

beforeEach(() => {
  vi.mocked(downloadFile).mockClear();
  vi.mocked(downloadFindingsCsv).mockClear();
  try {
    window.localStorage.clear();
  } catch {
    // no storage in this environment
  }
});

const OPUS_HIGH: ChatChoice = { model: OPUS.id, effort: 'high', thinking: true };
const FINDINGS = [finding('F1', { item: 'Vanity (left) top width' }), finding('F2', { item: 'Pantry counter depth', status: 'check' })];
const MARKUP: ChatFile = { id: 'f-m', name: 'marked-up.pdf', size: 2048, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-m' };

function view(change: Partial<ViewMessage>): ViewMessage {
  return {
    key: 'k', id: 'a-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: OPUS.id, effort: 'high', thinking: true,
    blocks: [], files: [], tasks: null, cost_usd: '0.50', stopped: null, error: null, status: 'done', ...change,
  } as ViewMessage;
}

describe('the + menu’s rules, without React', () => {
  it('finds, marks and steps between the blanks', () => {
    const text = 'Check page [page number] in the [area], then [page number].';
    expect(firstBlank(text)).toBe('[page number]');
    expect(firstBlank('Check page 4')).toBeNull();
    expect(firstBlank('My own [note] stays mine')).toBeNull();
    expect(blankRanges(text)).toEqual([[11, 24], [32, 38], [45, 58]]);
    expect(nextBlank(text, 0)).toEqual([11, 24]);
    expect(nextBlank(text, 24)).toEqual([32, 38]);
    expect(nextBlank(text, 59)).toEqual([11, 24]); // wraps round
    expect(nextBlank(text, 32, true)).toEqual([11, 24]);
    expect(nextBlank(text, 0, true)).toEqual([45, 58]);
    expect(blankSegments('a [area] b')).toEqual([{ text: 'a ', blank: false }, { text: '[area]', blank: true }, { text: ' b', blank: false }]);
  });

  it('adds a prompt after the reviewer’s own words, never over them', () => {
    expect(withPrompt('', 'Summarise.')).toBe('Summarise.');
    expect(withPrompt('  ', 'Summarise.')).toBe('Summarise.');
    expect(withPrompt('Only page 4 please.  ', 'Summarise.')).toBe('Only page 4 please.\n\nSummarise.');
  });

  it('holds the must-have options, each with words a reviewer can send', () => {
    const ids = plusActions().map((action) => action.id);
    // #1547: the products' Focus options are the registry's: each active one, both, then the ones
    // not ready yet ("coming later").
    expect(ids).toEqual([
      'full_check', 'check_page', 'compare_area', 'gv_rules', 'consistency', 'strings', 'missing', 'rounds',
      'cloud', 'csv', 'letter', 'rfi', 'summary',
      'product:countertop', 'product:cabinet', 'all_products',
      'product:lighting', 'product:seating', 'product:case_goods', 'product:mirrors', 'product:accessories', 'product:artwork',
      'quick', 'thorough',
    ]);
    expect(plusAction('all_products')?.label).toBe('Countertops and cabinets');
    expect(plusAction('product:lighting')?.run).toEqual({ kind: 'products', products: ['lighting'], comingLater: true });
    for (const action of PLUS_ACTIONS) {
      if (action.run.kind === 'prompt') expect(action.run.text.length).toBeGreaterThan(40);
    }
    expect(areaPrompt('Pantry')).toMatch(/^Compare the Pantry on the vendor’s shop drawing/);
  });

  it('says the full check’s time from one constant, and the cap it is given', () => {
    expect(FULL_CHECK_MINUTES).toBe(15);
    expect(fullCheckEstimate('$15')).toBe('~15 min · up to $15');
    expect(fullCheckEstimate(null)).toBe('~15 min');
  });

  it('remembers the three most recent options, newest first', () => {
    const storage = window.localStorage;
    rememberRecent(storage, 'csv');
    rememberRecent(storage, 'letter');
    rememberRecent(storage, 'check_page');
    rememberRecent(storage, 'csv');
    expect(readRecent(storage)).toEqual(['csv', 'check_page', 'letter']);
    rememberRecent(storage, 'rfi');
    expect(readRecent(storage)).toEqual(['rfi', 'csv', 'check_page']);
    storage.setItem('gv-chat-plus-recent', '["gone", 5, "summary"]');
    expect(readRecent(storage)).toEqual(['summary']);
    storage.setItem('gv-chat-plus-recent', 'not json');
    expect(readRecent(storage)).toEqual([]);
    expect(readRecent(null)).toEqual([]);
  });

  it('picks the effort Quick and Thorough set, within what the model allows', () => {
    expect(effortFor([OPUS], OPUS_HIGH, 'quick')).toBe('low');
    expect(effortFor([OPUS], OPUS_HIGH, 'thorough')).toBe('xhigh');
    // Sonnet with thinking off stops at High.
    expect(effortFor([SONNET], { model: SONNET.id, effort: 'medium', thinking: false }, 'thorough')).toBe('high');
    expect(sameProducts(['cabinet', 'countertop'], ['countertop', 'cabinet'])).toBe(true);
    expect(sameProducts(['countertop'], ['countertop', 'cabinet'])).toBe(false);
    expect(sameProducts(null, ['countertop'])).toBe(false);
  });

  it('finds the newest finished answer with findings, and the areas it named', () => {
    const messages = [
      view({ key: 'a', id: 'a-1', findings: FINDINGS }),
      view({ key: 'q', id: 'u-2', role: 'user', text: 'More?' }),
      view({ key: 'b', id: 'a-2', findings: [] }),
      view({ key: 'c', id: null, status: 'running', findings: FINDINGS }),
    ];
    expect(lastFindings(messages)).toEqual({ messageId: 'a-1', findings: FINDINGS });
    expect(lastFindings([])).toBeNull();
    expect(knownAreas(messages)).toEqual(['Vanity (left)', 'Pantry']);
  });
});

/** The composer with the menu, holding its own words, chips and choice like the page does. */
function Harness(props: Partial<PlusMenuProps> & { initial?: string; onSend?: (text: string) => void }) {
  const [value, setValue] = useState(props.initial ?? '');
  const [products, setProducts] = useState<ChatProduct[] | null>(['countertop']);
  const [choice, setChoice] = useState<ChatChoice>(OPUS_HIGH);
  const ref = useRef<HTMLTextAreaElement>(null);
  return (
    <ChatComposer
      value={value}
      onChange={setValue}
      onSend={() => props.onSend?.(value)}
      onStop={vi.fn()}
      running={false}
      stopping={false}
      blocked={null}
      models={[OPUS]}
      choice={choice}
      onChoice={setChoice}
      placeholder="Reply to Claude…"
      textareaRef={ref}
      leading={<span data-testid="chips">{products?.join('+') ?? 'none'}</span>}
      plus={
        <PlusMenu
          onPrompt={(text) => setValue((now) => withPrompt(now, text))}
          textareaRef={ref}
          fullCheck={{ summary: 'Full check · Countertops · Opus 5.5 · High · up to $5', blocked: null, onRun: vi.fn() }}
          lastFindings={{ messageId: 'a-1', findings: FINDINGS }}
          makeMarkupPdf={vi.fn(async () => MARKUP)}
          fileHref={(file) => file.url}
          products={products}
          onProducts={setProducts}
          models={[OPUS]}
          choice={choice}
          onChoice={setChoice}
          messageCap="$5"
          areas={['Vanity (left)', 'Pantry']}
          onNotice={vi.fn()}
          running={false}
          {...props}
        />
      }
    />
  );
}

const plusButton = () => screen.getByRole('button', { name: 'Add a check or output' });
const box = () => screen.getByRole('textbox', { name: 'Your message' }) as HTMLTextAreaElement;
const search = () => screen.getByPlaceholderText('Search checks and outputs');
/** An option by its value (a recent one is `recent:<id>`). */
const opt = (value: string) => {
  const found = document.querySelector<HTMLElement>(`[cmdk-item][data-value="${value}"]`);
  if (!found) throw new Error(`No option ${value}`);
  return found;
};

describe('the + menu', () => {
  it('opens upward with a search box, grouped Checks · Outputs · Focus, and estimates', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(plusButton());
    expect(document.activeElement).toBe(search());
    const menu = screen.getByRole('dialog', { name: 'Checks, outputs and focus' });
    expect(within(menu).getByText('Checks')).toBeTruthy();
    expect(within(menu).getByText('Outputs')).toBeTruthy();
    expect(within(menu).getByText('Focus')).toBeTruthy();
    expect(within(menu).getAllByRole('option')).toHaveLength(plusActions().length);
    // The products not ready yet are listed, greyed: "Coming later".
    expect(opt('product:lighting').getAttribute('aria-disabled')).toBe('true');
    expect(opt('product:lighting').textContent).toContain('Coming later');
    const full = opt('full_check');
    expect(full.textContent).toContain('~15 min · up to $5');
    expect(opt('check_page').textContent).toContain('~2 min · ~$0.50');
    expect(opt('cloud').textContent).toContain('$0');
    // The products already picked are marked.
    expect(within(opt('product:countertop')).getByLabelText('Current')).toBeTruthy();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(plusButton());
  });

  it('searches by name and by other words', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(plusButton());
    await user.type(search(), 'rfi');
    expect(screen.getAllByRole('option').map((option) => option.getAttribute('data-action'))).toEqual(['rfi']);
    await user.clear(search());
    await user.type(search(), 'diff');
    expect(screen.getAllByRole('option').map((option) => option.getAttribute('data-action'))).toContain('rounds');
    await user.clear(search());
    await user.type(search(), 'zzzz');
    expect(screen.getByText(/Nothing matches/)).toBeTruthy();
  });

  it('works from the keyboard: arrows move, Enter picks', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(plusButton());
    expect(opt('full_check').getAttribute('aria-selected')).toBe('true');
    await user.keyboard('{ArrowDown}');
    expect(opt('check_page').getAttribute('aria-selected')).toBe('true');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(document.activeElement).toBe(box()));
    expect(box().value).toMatch(/^Check vendor page \[page number\] against the architect’s drawing\./);
  });

  it('fills a prompt with its blank picked, highlighted, and Send waiting for it', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    await user.click(plusButton());
    await user.click(opt('check_page'));
    await waitFor(() => expect(document.activeElement).toBe(box()));
    const start = box().value.indexOf('[page number]');
    expect([box().selectionStart, box().selectionEnd]).toEqual([start, start + '[page number]'.length]);
    expect(document.querySelector('[data-slot=chat-composer-blanks] mark')?.textContent).toContain('[page number]');
    expect(screen.getByText(/Fill in \[page number\] to send/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true);
    await user.keyboard('{Enter}');
    expect(onSend).not.toHaveBeenCalled();
    // Typing over the picked blank fills it; Send is on again.
    await user.keyboard('12');
    expect(box().value).toMatch(/^Check vendor page 12 against/);
    expect(document.querySelector('[data-slot=chat-composer-blanks]')).toBeNull();
    expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(false);
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith(expect.stringMatching(/^Check vendor page 12 against/));
  });

  it('Tab moves between blanks, and leaves the box when none are left', async () => {
    const user = userEvent.setup();
    render(<Harness initial="Page [page number] in the [area]." />);
    box().focus();
    box().setSelectionRange(0, 0);
    await user.keyboard('{Tab}');
    expect([box().selectionStart, box().selectionEnd]).toEqual([5, 18]);
    await user.keyboard('{Tab}');
    expect([box().selectionStart, box().selectionEnd]).toEqual([26, 32]);
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect([box().selectionStart, box().selectionEnd]).toEqual([5, 18]);
  });

  it('keeps the reviewer’s words and adds the prompt after them', async () => {
    const user = userEvent.setup();
    render(<Harness initial="Only the kitchen." />);
    await user.click(plusButton());
    await user.click(opt('summary'));
    expect(box().value).toBe('Only the kitchen.\n\nSummarise this drawing set in two lines: what it covers, and the most important problems found so far.');
  });

  it.each([
    ['Check GV rules', /GV’s rules for the selected products\. For each rule/],
    ['Do the dimension strings add up?', /^Add up every dimension string on the vendor’s pages/],
    ['What’s missing on either side?', /^What is missing on either side\?/],
    ['What changed between rounds?', /^What changed between round 1 and round 2\?/],
    ['Comments letter to the vendor', /^Write a comments letter to the vendor .* the page, the item and what to change/],
    ['Questions for the architect', /^Write the questions for the architect \(an RFI list\)/],
    ['Two-line summary', /^Summarise this drawing set in two lines/],
  ])('“%s” fills the composer with its prompt', async (label, words) => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(plusButton());
    await user.click(screen.getAllByRole('option').find((option) => option.textContent?.startsWith(label))!);
    expect(box().value).toMatch(words);
    expect(firstBlank(box().value)).toBeNull();
  });

  it('Compare an area… offers the chat’s areas, or a blank to type one', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(plusButton());
    await user.click(opt('compare_area'));
    const areas = screen.getByPlaceholderText('Search areas');
    expect(document.activeElement).toBe(areas);
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(['Vanity (left)', 'Pantry', 'Type another area…']);
    await user.click(opt('area:Pantry'));
    expect(box().value).toBe(areaPrompt('Pantry'));

    await user.click(plusButton());
    await user.click(opt('compare_area'));
    await user.click(opt('area:other'));
    await waitFor(() => expect(document.activeElement).toBe(box()));
    expect(box().value).toContain('Compare the [area] on the vendor’s shop drawing');
    const at = box().value.lastIndexOf('[area]');
    expect([box().selectionStart, box().selectionEnd]).toEqual([at, at + 6]);
  });

  it('Compare an area… goes straight to the blank when no area is known, and Back returns from the area list', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness areas={[]} />);
    await user.click(plusButton());
    await user.click(opt('compare_area'));
    expect(box().value).toContain('[area]');
    unmount();

    render(<Harness />);
    await user.click(plusButton());
    await user.click(opt('compare_area'));
    await user.click(screen.getByRole('button', { name: 'Back to all actions' }));
    expect(search()).toBeTruthy();
  });

  it('Focus sets the product chips and the effort', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByTestId('chips').textContent).toContain('countertop');
    await user.click(plusButton());
    await user.click(opt('product:cabinet'));
    expect(screen.getByTestId('chips').textContent).toMatch(/^cabinet$/);
    await user.click(plusButton());
    await user.click(opt('all_products'));
    expect(screen.getByTestId('chips').textContent).toContain('countertop+cabinet');

    await user.click(plusButton());
    expect(opt('quick').textContent).toContain('Low effort');
    await user.click(opt('quick'));
    await user.click(plusButton());
    expect(within(opt('quick')).getByLabelText('Current')).toBeTruthy();
    await user.click(opt('thorough'));
    await user.click(plusButton());
    expect(opt('thorough').textContent).toContain('Extra high effort');
    expect(within(opt('thorough')).getByLabelText('Current')).toBeTruthy();
  });

  it('the full check is confirmed first with the same line as the button, then runs', async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<Harness fullCheck={{ summary: 'Full check · Countertops · Opus 5.5 · High · up to $5', blocked: null, onRun }} />);
    await user.click(plusButton());
    await user.click(opt('full_check'));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByText('Full check · Countertops · Opus 5.5 · High · up to $5')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Back' }));
    await user.click(opt('full_check'));
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('says why an option cannot be used now', async () => {
    const user = userEvent.setup();
    render(<Harness lastFindings={null} fullCheck={{ summary: 'Full check', blocked: 'A message is still running in this chat.', onRun: vi.fn() }} />);
    await user.click(plusButton());
    expect(opt('full_check').getAttribute('aria-disabled')).toBe('true');
    expect(opt('full_check').textContent).toContain('A message is still running');
    expect(opt('cloud').getAttribute('aria-disabled')).toBe('true');
    expect(opt('cloud').textContent).toContain('After an answer with findings');
    expect(opt('csv').getAttribute('aria-disabled')).toBe('true');
  });

  it('the cloud drawing uses the existing markup-pdf call on the last answer, then downloads it ($0)', async () => {
    const user = userEvent.setup();
    const download = vi.mocked(downloadFile);
    const makeMarkupPdf = vi.fn(async () => MARKUP);
    render(<Harness makeMarkupPdf={makeMarkupPdf} />);
    await user.click(plusButton());
    await user.click(opt('cloud'));
    expect(makeMarkupPdf).toHaveBeenCalledWith('a-1');
    await waitFor(() => expect(download).toHaveBeenCalledWith(MARKUP.url, 'marked-up.pdf'));
    expect(document.querySelector('[data-slot=chat-plus-status]')?.textContent).toContain('Cloud drawing ready');
  });

  it('a cloud drawing that cannot be made is said plainly', async () => {
    const user = userEvent.setup();
    const onNotice = vi.fn();
    render(<Harness makeMarkupPdf={vi.fn(async () => Promise.reject(new TypeError('offline')))} onNotice={onNotice} />);
    await user.click(plusButton());
    await user.click(opt('cloud'));
    await waitFor(() => expect(onNotice).toHaveBeenCalledWith('The server could not be reached. Check the connection and try again.'));
  });

  it('the Excel uses the existing CSV export of the last answer’s findings ($0)', async () => {
    const user = userEvent.setup();
    const csv = vi.mocked(downloadFindingsCsv);
    render(<Harness />);
    await user.click(plusButton());
    await user.click(opt('csv'));
    expect(csv).toHaveBeenCalledWith(FINDINGS, 'findings-a-1.csv');
  });

  it('the vendor consistency check runs by code ($0) and says when it is done', async () => {
    const user = userEvent.setup();
    let finish: () => void = () => {};
    const onRun = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<Harness consistency={{ onRun }} />);
    await user.click(plusButton());
    expect(opt('consistency').textContent).toContain('$0');
    await user.click(opt('consistency'));
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(plusButton().getAttribute('aria-busy')).toBe('true');
    finish();
    await waitFor(() => expect(document.querySelector('[data-slot=chat-plus-status]')?.textContent).toContain('Vendor consistency check finished'));
    expect(plusButton().getAttribute('aria-busy')).toBeNull();
  });

  it('the vendor consistency check waits while an answer runs, and is absent where it cannot run', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness consistency={{ onRun: vi.fn(async () => {}) }} running />);
    await user.click(plusButton());
    expect(opt('consistency').getAttribute('aria-disabled')).toBe('true');
    expect(opt('consistency').textContent).toContain('After this answer');
    unmount();
    render(<Harness />);
    await user.click(plusButton());
    expect(opt('consistency').textContent).toContain('Not available here');
  });

  it('lists the recently used options first', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(plusButton());
    await user.click(opt('summary'));
    await user.click(plusButton());
    const recent = screen.getByRole('group', { name: 'Recent' });
    expect(within(recent).getAllByRole('option').map((option) => option.getAttribute('data-action'))).toEqual(['summary']);
    // While searching, only the groups show (no repeats).
    await user.type(search(), 'summary');
    expect(screen.getAllByRole('option')).toHaveLength(1);
  });
});

describe('the + menu on the chat page', () => {
  const ANSWER: ChatMessage = {
    id: 'a-1', role: 'assistant', created_at: '2026-10-17T09:03:00Z', text: 'Two items differ.', model: OPUS.id, effort: 'high', thinking: true, cost_usd: '0.50', stopped: null,
    blocks: [{ type: 'text', text: 'Two items differ.' }, ...FINDINGS.map((each) => ({ type: 'finding', finding: each }))] as never, files: [],
  };

  function renderPage(api: ChatApi) {
    const chats = document.body.appendChild(document.createElement('div'));
    render(
      <ShellSlotsContext.Provider value={{ title: null, actions: null, chats }}>
        <ChatPage packageId="pkg-1" chatId="c-1" api={api} onOpenChat={vi.fn()} stopGraceMs={0} />
      </ShellSlotsContext.Provider>,
    );
  }

  it('fills the composer, sets the chips (remembered per set) and makes the cloud drawing from the last answer', async () => {
    const user = userEvent.setup();
    const api = fakeChatApi({ get: vi.fn(async (id: string) => chatOut(id, [QUESTION, ANSWER], { product_type: 'countertop' })) as never });
    renderPage(api);
    await screen.findByText('Two items differ.');
    const chips = screen.getByRole('group', { name: 'Products to check' });
    await waitFor(() => expect(within(chips).getByRole('button', { name: 'Countertop' }).getAttribute('aria-pressed')).toBe('true'));

    await user.click(plusButton());
    await user.click(opt('product:cabinet'));
    expect(within(chips).getByRole('button', { name: 'Cabinet' }).getAttribute('aria-pressed')).toBe('true');
    expect(within(chips).getByRole('button', { name: 'Countertop' }).getAttribute('aria-pressed')).toBe('false');
    expect(window.localStorage.getItem('gv-chat-products:pkg-1')).toBe('["cabinet"]');

    await user.click(plusButton());
    await user.click(opt('rfi'));
    expect(box().value).toMatch(/^Write the questions for the architect/);

    await user.click(plusButton());
    await user.click(opt('cloud'));
    expect(api.markup).toHaveBeenCalledWith('c-1', 'a-1');
  });

  it('shows the per-answer cap the server states, in the words’ face (no monospace)', async () => {
    const user = userEvent.setup();
    const api = fakeChatApi({
      models: vi.fn(async () => modelsOut({ message_cap_usd: '15.00' })) as never,
      get: vi.fn(async (id: string) => chatOut(id, [QUESTION, ANSWER], { product_type: 'countertop' })) as never,
    });
    renderPage(api);
    await screen.findByText('Two items differ.');
    await user.click(plusButton());
    const estimate = opt('full_check').querySelector('[data-slot=chat-plus-estimate]');
    expect(estimate?.textContent).toBe('~15 min · up to $15');
    expect(opt('check_page').querySelector('[data-slot=chat-plus-estimate]')?.textContent).toBe('~2 min · ~$0.50');
    for (const hint of document.querySelectorAll('[data-slot=chat-plus-estimate], [data-slot=chat-plus-estimate] *')) {
      expect(hint.className).not.toMatch(/\b(num|font-mono)\b/);
    }
    expect(estimate?.querySelector('.tabular-nums')).toBeTruthy();
  });

  it('runs the vendor consistency check by code and shows its stored answer; Retry runs it again, never the AI', async () => {
    const user = userEvent.setup();
    const CHECKED: ChatMessage = {
      id: 'a-2', role: 'assistant', created_at: '2026-10-17T09:05:00Z', text: 'Vendor consistency (by code, $0): 4 dimension strings added up, 1 does not.',
      model: 'code-check', effort: '' as never, thinking: false, cost_usd: '0.00', stopped: null,
      blocks: [{ type: 'text', text: 'Vendor consistency (by code, $0): 4 dimension strings added up, 1 does not.', by: 'code' }] as never, files: [],
    };
    const ASKED: ChatMessage = { id: 'q-2', role: 'user', created_at: '2026-10-17T09:05:00Z', text: 'Vendor consistency check' } as ChatMessage;
    let stored = [QUESTION, ANSWER];
    const api = fakeChatApi({
      get: vi.fn(async (id: string) => chatOut(id, stored, { product_type: 'countertop' })) as never,
      consistency: vi.fn(async () => {
        stored = [QUESTION, ANSWER, ASKED, CHECKED];
        return CHECKED;
      }) as never,
    });
    renderPage(api);
    await screen.findByText('Two items differ.');
    await user.click(plusButton());
    await user.click(opt('consistency'));
    expect(api.consistency).toHaveBeenCalledWith('c-1');
    expect(await screen.findByText(/4 dimension strings added up, 1 does not/)).toBeTruthy();
    expect(api.send).not.toHaveBeenCalled();
    const retries = screen.getAllByRole('button', { name: 'Retry' });
    await user.click(retries[retries.length - 1]);
    await waitFor(() => expect(api.consistency).toHaveBeenCalledTimes(2));
    expect(api.send).not.toHaveBeenCalled();
  });

  it('runs the full check through the page’s own send, after the confirm', async () => {
    const user = userEvent.setup();
    const api = fakeChatApi({ get: vi.fn(async (id: string) => chatOut(id, [QUESTION, ANSWER], { product_type: 'countertop' })) as never });
    renderPage(api);
    await screen.findByText('Two items differ.');
    await user.click(plusButton());
    await user.click(opt('full_check'));
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    await waitFor(() => expect(api.send).toHaveBeenCalled());
    expect(api.send.mock.calls[0][1]).toMatchObject({ text: 'Full check', mode: 'full_check', products: ['countertop'], model: OPUS.id, effort: 'high' });
  });
});
