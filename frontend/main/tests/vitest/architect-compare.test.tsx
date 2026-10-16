// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import type { ArchitectCompareCounts, ArchitectCompareEntry, ReviewItemsOut } from '@/api/review-items';
import { ArchitectCompare } from '@/components/simple-review/architect-compare';
import { compareSummary } from '@/components/simple-review/architect-compare-lib';
import { reviewItems } from './fixtures/review-items';

// Synthetic data only: every number, page and view here is invented.
vi.mock('@/api/config', () => ({ projectId: () => 'proj-demo' }));

const { SimpleReviewPage } = await import('@/pages/SimpleReviewPage');

const ref = (dv: string, page: number) => ({ document_version_id: dv, page_number: page, polygon: [['0.1', '0.1'], ['0.2', '0.1'], ['0.2', '0.2'], ['0.1', '0.2']] });

function entry(id: string, result: ArchitectCompareEntry['result'], extra: Partial<ArchitectCompareEntry> = {}): ArchitectCompareEntry {
  const paired = result === 'same' || result === 'different' || result === 'rule';
  return {
    key: `arch:${id}`,
    page: 3,
    view: 'Sample elevation A',
    architect_text: '30"',
    architect_inches: '30/1',
    vendor_text: paired ? (result === 'different' ? '30 1/2"' : '30"') : null,
    vendor_inches: paired ? (result === 'different' ? '61/2' : '30/1') : null,
    result,
    rule_id: result === 'rule' ? 'CT-SAMPLE-001' : null,
    rule_outcome: result === 'rule' ? 'PASS' : null,
    why: result === 'unread' ? 'The architect number could not be read.' : result === 'unpaired' ? 'No vendor number sits at this spot.' : 'Compared exactly.',
    finding_id: null,
    architect_ref: ref('docv-arch', 4),
    vendor_ref: paired ? ref('docv-vendor', 3) : null,
    ...extra,
  };
}

const ENTRIES: ArchitectCompareEntry[] = [
  entry('s1', 'same'),
  entry('d1', 'different'),
  entry('s2', 'same'),
  entry('r1', 'rule'),
  entry('u1', 'unpaired'),
  entry('n1', 'unread'),
  entry('s3', 'same'),
];
// Deliberately not the list's own counts: the line prints what the server sent.
const COUNTS: ArchitectCompareCounts = { same: 80, different: 3, rule: 6, unpaired: 4, unread: 1 };

describe('ArchitectCompare', () => {
  it('prints the one-line summary from the server counts', () => {
    render(<ArchitectCompare entries={ENTRIES} counts={COUNTS} onLook={() => {}} />);
    expect(screen.getByRole('heading', { level: 2, name: 'Compared with the architect' })).toBeTruthy();
    expect(screen.getByTestId('compare-summary').textContent).toBe('94 numbers compared: 80 same · 3 different · 6 checked by a rule · 5 need you');
    expect(within(screen.getByTestId('compare-summary')).getByText('94').className).toContain('num');
  });

  it('counts the list itself when the header sent no counts', () => {
    render(<ArchitectCompare entries={ENTRIES} counts={null} onLook={() => {}} />);
    expect(screen.getByTestId('compare-summary').textContent).toBe('7 numbers compared: 3 same · 1 different · 1 checked by a rule · 2 need you');
  });

  it('opens Different and Need you, folds the rule and Same groups', async () => {
    render(<ArchitectCompare entries={ENTRIES} counts={COUNTS} onLook={() => {}} />);
    const different = screen.getByRole('button', { name: /^Different/ });
    expect(different.getAttribute('aria-expanded')).toBe('true');
    const list = screen.getByRole('list', { name: 'Different' });
    const row = within(list).getByRole('listitem');
    // Glyph and word, never colour alone; the numbers in the number face.
    expect(within(row).getByText('Different').querySelector('svg')).toBeTruthy();
    expect(within(row).getByText('30 1/2"').className).toContain('num');
    expect(row.textContent).toContain('Vendor 30 1/2" vs architect 30"');
    expect(row.textContent).toContain('p.3');

    expect(screen.getByRole('button', { name: /^Need you/ }).getAttribute('aria-expanded')).toBe('true');
    const need = screen.getByRole('list', { name: 'Need you' });
    expect(need.textContent).toContain('No vendor number sits at this spot.');
    expect(need.textContent).toContain('The architect number could not be read.');
    expect(within(need).getByText('No match')).toBeTruthy();
    expect(within(need).getByText('Not read')).toBeTruthy();

    const same = screen.getByRole('button', { name: /^Same/ });
    expect(same.getAttribute('aria-expanded')).toBe('false');
    expect(same.textContent).toContain('Show all 3');
    expect(screen.queryByRole('list', { name: 'Same' })).toBeNull();
    await userEvent.click(same);
    expect(within(screen.getByRole('list', { name: 'Same' })).getAllByRole('listitem')).toHaveLength(3);

    const rule = screen.getByRole('button', { name: /^Checked by a rule/ });
    expect(rule.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(rule);
    const ruled = screen.getByRole('list', { name: 'Checked by a rule' });
    expect(ruled.textContent).toContain('CT-SAMPLE-001: looks right');
  });

  it('Look hands back the entry it was pressed on, from the keyboard too', async () => {
    const onLook = vi.fn();
    render(<ArchitectCompare entries={ENTRIES} counts={COUNTS} onLook={onLook} />);
    const look = screen.getByRole('button', { name: 'Look at Vendor 30 1/2" vs architect 30", page 3' });
    look.focus();
    await userEvent.keyboard('{Enter}');
    expect(onLook).toHaveBeenCalledWith(expect.objectContaining({ key: 'arch:d1' }));
  });

  it('is not shown with an empty list (an older package)', () => {
    const { container } = render(<ArchitectCompare entries={[]} counts={null} onLook={() => {}} />);
    expect(container.innerHTML).toBe('');
  });

  it('says one number in the singular', () => {
    expect(compareSummary({ same: 0, different: 1, rule: 0, unpaired: 0, unread: 0 })).toBe('1 number compared: 0 same · 1 different · 0 checked by a rule · 0 need you');
  });
});

describe('SimpleReviewPage: Compared with the architect', () => {
  const API = '*/api/v1/projects/proj-demo/packages/pkg-demo';
  let current: ReviewItemsOut;
  const server = setupServer(
    http.get(`${API}/review-items`, () => HttpResponse.json(current)),
    http.get(API, () => HttpResponse.json({ id: 'pkg-demo', vendor: 'Sample Stoneworks', current_revision_number: 2 })),
  );
  beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
  afterEach(() => server.resetHandlers());
  afterAll(() => server.close());

  function withCompare(): ReviewItemsOut {
    return reviewItems((draft) => {
      Object.assign(draft, { architect_compare: ENTRIES });
      Object.assign(draft.header, { architect_compare_counts: COUNTS });
    });
  }

  function renderPage() {
    const onOpenPdfs = vi.fn();
    render(<SimpleReviewPage packageId="pkg-demo" pictures={async () => 'data:image/png;base64,iVBORw0KGgo='} assistantAvailable={false} onOpenAdvanced={vi.fn()} onOpenPdfs={onOpenPdfs} />);
    return onOpenPdfs;
  }

  it('is the first section, and Look opens both drawings with both numbers ringed', async () => {
    current = withCompare();
    const onOpenPdfs = renderPage();
    const section = await screen.findByRole('region', { name: 'Compared with the architect' });
    const failures = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    expect(section.compareDocumentPosition(failures) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(within(section).getByRole('button', { name: /^Look at Vendor 30 1\/2"/ }));
    expect(onOpenPdfs).toHaveBeenCalledWith(
      expect.objectContaining({
        key: 'arch:d1',
        page: 3,
        vendor_ref: expect.objectContaining({ document_version_id: 'docv-vendor', page_number: 3 }),
        architect_ref: expect.objectContaining({ location: expect.objectContaining({ document_version_id: 'docv-arch', page_number: 4 }) }),
      }),
    );
  });

  it('an unpaired number opens with only the architect spot, and says the vendor has none', async () => {
    current = withCompare();
    const onOpenPdfs = renderPage();
    const section = await screen.findByRole('region', { name: 'Compared with the architect' });
    await userEvent.click(within(section).getAllByRole('button', { name: /^Look at Architect 30"/ })[0]);
    const at = onOpenPdfs.mock.calls[0][0];
    expect(at.vendor_ref).toBeNull();
    expect(at.architect_ref.location.document_version_id).toBe('docv-arch');
  });

  it('an older package with no list shows no section', async () => {
    current = reviewItems();
    renderPage();
    await screen.findByTestId('ai-read-line');
    expect(screen.queryByRole('region', { name: 'Compared with the architect' })).toBeNull();
  });
});
