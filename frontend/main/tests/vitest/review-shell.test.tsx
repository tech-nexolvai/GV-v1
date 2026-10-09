// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { reviewStage, type ReviewFacts } from '@/lib/review-stage';
import { changedValuesSummary, parseChangedValue } from '@/lib/changed-values';
import { ReviewStepper } from '@/components/review/review-stepper';
import { NextActionButton } from '@/components/review/next-action';
import { ChangedValuesBadge } from '@/components/review/changed-values-badge';
import { RecordIdsDialog } from '@/components/review/record-ids-dialog';
import { TooltipProvider } from '@/components/ui/tooltip';
import { HeaderActions, HeaderTitleExtra } from '@/components/shell/ShellHeader';

// The shell's sidebar asks the API for the reviews list; these tests run without a server.
vi.mock('@/api/config', () => ({ projectId: () => '00000000-0000-4000-8000-000000000000' }));
vi.mock('@/api/client', () => ({
  listPackages: vi.fn(async () => ({
    items: [
      { id: 'p1', vendor: 'Synthetic vendor A', state: 'AWAITING_REVIEW', created_at: new Date().toISOString(), current_revision_number: 1, current_revision_id: 'r1', project_id: 'x', product_type: 'countertop' },
      { id: 'p2', vendor: 'Synthetic vendor B', state: 'APPROVED', created_at: new Date().toISOString(), current_revision_number: 2, current_revision_id: 'r2', project_id: 'x', product_type: 'countertop' },
    ],
    next_cursor: null,
  })),
  getApprovalReadiness: vi.fn(async () => ({ revision_id: 'r1', can_approve: false, blocking_findings: 4, blocking_finding_ids: [], reason: 'synthetic' })),
}));

const { AppShell, COLLAPSED_KEY } = await import('@/components/shell/AppShell');

const base: ReviewFacts = { state: 'AWAITING_REVIEW', findingsTotal: 9, readiness: { blockingFindings: 3, canApprove: false, reason: '3 findings need a decision' }, exports: null };

function Stepper(facts: ReviewFacts) {
  return (
    <TooltipProvider>
      <ReviewStepper steps={reviewStage(facts).steps} />
    </TooltipProvider>
  );
}

function NextAction({ facts, onAct = () => {}, extraDisabled }: { facts: ReviewFacts; onAct?: (kind: string) => void; extraDisabled?: boolean }) {
  return (
    <TooltipProvider>
      <NextActionButton action={reviewStage(facts).next} onAct={onAct} extraDisabled={extraDisabled} secondary={[{ id: 'refresh', label: 'Refresh results', onSelect: () => {} }]} />
    </TooltipProvider>
  );
}

describe('review stepper', () => {
  it('shows six steps, marks the current one, and says each state to screen readers', () => {
    render(<Stepper {...base} />);
    const nav = screen.getByRole('navigation', { name: 'Review progress' });
    const items = within(nav).getAllByRole('listitem');
    expect(items).toHaveLength(6);
    expect(items[3].getAttribute('aria-current')).toBe('step');
    expect(nav.textContent).toContain('Step 3 of 6: Checks, done, 9 recorded');
    expect(nav.textContent).toContain('Step 4 of 6: Decisions, current step');
    expect(nav.textContent).toContain('Step 6 of 6: Report, not started');
  });

  it('the Decisions step shows where the review stands, not how many need you (that is on the header and the tab, #1126)', () => {
    render(<Stepper {...base} readiness={{ blockingFindings: 15, canApprove: false, reason: '15 findings need a decision' }} />);
    const nav = screen.getByRole('navigation', { name: 'Review progress' });
    const decisions = nav.querySelector('[data-step="decisions"]') as HTMLElement;
    expect(decisions.getAttribute('data-state')).toBe('current');
    expect(decisions.textContent).not.toContain('15');
    expect(nav.textContent).not.toContain('15');
    // The checks step keeps its own, different number.
    expect(nav.querySelector('[data-step="checks"]')?.textContent).toContain('9');
  });

  it('a blocked step says why, and the reason is reachable by keyboard', async () => {
    const user = userEvent.setup();
    render(<Stepper {...base} readiness={{ blockingFindings: 0, canApprove: false, reason: 'An earlier sign-off is still open' }} />);
    const blocked = document.querySelector('[data-step="signoff"]');
    expect(blocked?.getAttribute('data-state')).toBe('blocked');
    expect(blocked?.textContent).toContain('An earlier sign-off is still open');
    await user.tab();
    expect(document.activeElement?.contains(blocked as Node)).toBe(true);
  });

  it('reading shows the pipeline stage, never an invented page count', () => {
    render(<Stepper {...base} state="EXTRACTING" findingsTotal={0} readiness={null} readingStage="EXTRACTING" />);
    const reading = document.querySelector('[data-step="reading"]');
    expect(reading?.getAttribute('data-state')).toBe('current');
    expect(reading?.textContent).toContain('extracting');
    expect(reading?.textContent).not.toMatch(/page \d+ of \d+/i);
  });
});

describe('next action', () => {
  it.each([
    [{ ...base, state: 'EXTRACTING', findingsTotal: 0, readiness: null }, 'Reading…', true],
    [{ ...base, state: 'NEEDS_INPUT', findingsTotal: 0, readiness: null }, 'Run checks', false],
    [{ ...base, state: 'RUNNING_CHECKS' }, 'Checking…', true],
    [base, 'Review 3 items', false],
    [{ ...base, readiness: { blockingFindings: 0, canApprove: true, reason: null } }, 'Sign off', false],
    [{ ...base, state: 'APPROVED', exports: 'preparing' as const }, 'Preparing report…', true],
    [{ ...base, state: 'APPROVED', exports: 'ready' as const }, 'Download report', false],
  ])('%#: %s → %s', (facts, label, disabled) => {
    render(<NextAction facts={facts as ReviewFacts} />);
    const button = screen.getByRole('button', { name: label as string });
    expect((button as HTMLButtonElement).disabled).toBe(disabled);
  });

  it('pressing it runs the action for that state', async () => {
    const user = userEvent.setup();
    const onAct = vi.fn();
    render(<NextAction facts={base} onAct={onAct} />);
    await user.click(screen.getByRole('button', { name: 'Review 3 items' }));
    expect(onAct).toHaveBeenCalledWith('review');
  });

  it('Sign off stays disabled, with the server reason, while anything blocks it', () => {
    render(<NextAction facts={{ ...base, readiness: { blockingFindings: 0, canApprove: false, reason: 'Two findings were changed after the run' } }} />);
    const button = screen.getByRole('button', { name: 'Sign off' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-describedby')).toBe('next-action-reason');
    expect(document.getElementById('next-action-reason')?.textContent).toBe('Two findings were changed after the run');
  });

  it('with N items blocking, the action is to review them, never to sign off', () => {
    render(<NextAction facts={{ ...base, readiness: { blockingFindings: 2, canApprove: false, reason: null } }} />);
    expect(screen.queryByRole('button', { name: 'Sign off' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Review 2 items' })).toBeTruthy();
  });

  it('the page can hold Sign off back while it is already being sent', () => {
    render(<NextAction facts={{ ...base, readiness: { blockingFindings: 0, canApprove: true, reason: null } }} extraDisabled />);
    expect((screen.getByRole('button', { name: 'Sign off' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('secondary actions sit in a small menu', async () => {
    const user = userEvent.setup();
    render(<NextAction facts={base} />);
    await user.click(screen.getByRole('button', { name: 'More actions' }));
    expect(await screen.findByRole('menuitem', { name: 'Refresh results' })).toBeTruthy();
  });
});

describe('changed values', () => {
  const revision = '00000000-0000-4000-8000-000000000101';
  const line = 'synthetic_offset = 4 1/2 in (PROJECT, typed on the form, set by reviewer); overrides 4 in (GLOBAL, company standard)';

  it('reads the server sentence into columns, and keeps any other shape whole', () => {
    expect(parseChangedValue(line)).toMatchObject({ kind: 'parsed', setting: 'synthetic_offset', standard: '4"', project: '4 1/2"', source: 'typed on the form · reviewer' });
    expect(parseChangedValue('something in another shape')).toEqual({ kind: 'raw', raw: 'something in another shape' });
  });

  it('the badge counts what differs, then what is missing, and never a count that did not load', () => {
    const value = { revision_id: revision, status: 'available', message: null, company_standards_displaced: [line, line, line], outstanding: ['x'] };
    expect(changedValuesSummary('ready', value)).toBe('3 project values differ');
    expect(changedValuesSummary('ready', { ...value, company_standards_displaced: [] })).toBe('1 value not set');
    expect(changedValuesSummary('ready', { ...value, company_standards_displaced: [], outstanding: [] })).toBe('GV standard values');
    expect(changedValuesSummary('error', null)).toBe('Project values');
  });

  it('opens a table with each part under its own heading, saying "none" only once each', async () => {
    const user = userEvent.setup();
    const value = { revision_id: revision, status: 'available', message: null, company_standards_displaced: [line], outstanding: [] };
    render(<ChangedValuesBadge state="ready" value={value} currentRevisionId={revision} />);
    await user.click(screen.getByRole('button', { name: /1 project value differs/ }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('columnheader', { name: 'GV standard' })).toBeTruthy();
    expect(within(dialog).getByRole('cell', { name: '4 1/2"' })).toBeTruthy();
    expect(within(dialog).getByRole('heading', { name: 'Required values not set' })).toBeTruthy();
    expect(dialog.textContent?.match(/None/g) ?? []).toHaveLength(0);
    expect(dialog.textContent).toContain('Every required value is set.');
  });
});

describe('record IDs', () => {
  it('replaces the old "Details & steps" dropdown with a dialog of the record identifiers', () => {
    render(<RecordIdsDialog open onOpenChange={() => {}} packageId="package-a" projectId="project-b" revisionId="revision-c" />);
    const dialog = screen.getByRole('dialog', { name: 'Record IDs' });
    expect(dialog.textContent).toContain('package-a');
    expect(dialog.textContent).toContain('project-b');
    expect(dialog.textContent).toContain('revision-c');
  });
});

describe('app shell', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  function Shell({ children, openPackage = 'p1' }: { children?: React.ReactNode; openPackage?: string | null }) {
    return (
      <AppShell
        title="Synthetic vendor A"
        crumbs={[{ label: 'Documents', onSelect: () => {} }, { label: 'Synthetic vendor A' }, { label: 'Revision 1' }]}
        activePage={openPackage ? 'review' : 'documents'}
        activePackage={openPackage}
        theme="light"
        sidebarRefreshKey={0}
        liveNeedYou={null}
        onNavigate={() => {}}
        onOpenPackage={() => {}}
        onNewReview={() => {}}
        onToggleTheme={() => {}}
      >
        {children}
      </AppShell>
    );
  }

  it('remembers a collapsed sidebar on this device', async () => {
    const user = userEvent.setup();
    localStorage.setItem(COLLAPSED_KEY, 'true');
    const { unmount } = render(<Shell />);
    const sidebar = document.querySelector('[data-slot="sidebar"][data-state]');
    expect(sidebar?.getAttribute('data-state')).toBe('collapsed');

    await user.click(screen.getByRole('button', { name: 'Toggle sidebar' }));
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('false');
    expect(document.querySelector('[data-slot="sidebar"][data-state]')?.getAttribute('data-state')).toBe('expanded');
    unmount();

    render(<Shell />);
    expect(document.querySelector('[data-slot="sidebar"][data-state]')?.getAttribute('data-state')).toBe('expanded');
  });

  it('lists recent reviews with their status and how many need you', async () => {
    render(<Shell openPackage={null} />);
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    expect(await within(sidebar).findByText('Synthetic vendor A')).toBeTruthy();
    await waitFor(() => expect(sidebar.querySelector('[data-slot="need-you"]')?.textContent).toBe('4 need you'));
    expect(document.querySelector('[data-status="APPROVED"]')).not.toBeNull();
  });

  it('the open review says only that it needs you: its number is on its header and tab (#1126)', async () => {
    render(<Shell />);
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    await waitFor(() => expect(sidebar.querySelector('[data-slot="need-you"]')?.textContent).toBe('Needs you'));
    expect(sidebar.querySelector('[data-slot="need-you"] [data-outcome-icon="REVIEW_REQUIRED"]')).not.toBeNull();
  });

  it('keyboard order: skip link, navigation, header (where you are → status → action), then the page', async () => {
    const user = userEvent.setup();
    render(
      <Shell>
        <HeaderTitleExtra>
          <button type="button">Project values</button>
        </HeaderTitleExtra>
        <HeaderActions>
          <TooltipProvider>
            <NextActionButton action={reviewStage(base).next} onAct={() => {}} />
          </TooltipProvider>
        </HeaderActions>
        <button type="button">Page content</button>
      </Shell>,
    );
    await within(document.querySelector('[data-slot="sidebar"]') as HTMLElement).findByText('Synthetic vendor A');

    const order: string[] = [];
    for (let i = 0; i < 20; i += 1) {
      await user.tab();
      const element = document.activeElement as HTMLElement;
      order.push(element.getAttribute('aria-label') || element.textContent?.trim() || element.tagName);
    }
    const at = (name: string) => order.findIndex((entry) => entry.includes(name));
    expect(at('Skip to content')).toBe(0);
    expect(at('New review')).toBeGreaterThan(at('Skip to content'));
    expect(at('Documents')).toBeGreaterThan(at('New review'));
    expect(at('Toggle sidebar')).toBeGreaterThan(at('Documents'));
    expect(at('Project values')).toBeGreaterThan(at('Toggle sidebar'));
    expect(at('Review 3 items')).toBeGreaterThan(at('Project values'));
    expect(at('Page content')).toBeGreaterThan(at('Review 3 items'));
  });
});

describe('sign-off waits for the server', () => {
  it('a slow readiness answer shows no count and no sign-off until it arrives', async () => {
    const { rerender } = render(<NextAction facts={{ ...base, readiness: null }} />);
    expect(screen.getByRole('button', { name: 'Review results' })).toBeTruthy();
    rerender(<NextAction facts={{ ...base, readiness: { blockingFindings: 0, canApprove: true, reason: null } }} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign off' })).toBeTruthy());
  });
});
