// @vitest-environment jsdom
/**
 * The depth and sink reader's readings on the screens (#1295): the waiting card's lines per
 * countertop, "Confirm this reading" through the existing confirm path, the view-kind chip on the
 * Measure page, and the vendor's sink note as a hint. Without readings every screen is as before.
 *
 * Synthetic data only (#1106): nothing here comes from a client drawing.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Finding } from '@/data/types';
import { confirmCandidate, downloadCandidateCrop, getProjectSink, getSinkInUse } from '@/api/client';
import { NeedsValues } from '@/components/results/needs-values';
import { OtherChecks } from '@/components/results/other-checks';
import { LinkedViewChips } from '@/components/measure/LinkedViewChips';
import { SinkEntryCard } from '@/components/measure/SinkEntry';
import { readingSentence, sinkNoteSentence, type DepthReading } from '@/lib/depth-readings';
import { SIGNED_OFF_NOTE, SignedOffContext } from '@/lib/signed-off';
import type { MissingInput } from '@/lib/waiting-values';

vi.mock('@/api/config', () => ({ projectId: () => 'project-1' }));
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  confirmCandidate: vi.fn(async () => ({ canonical_observation_id: 'obs-1', semantic_type: 'CT010', status: 'HUMAN_CONFIRMED' })),
  downloadCandidateCrop: vi.fn(async () => new Blob(['synthetic'], { type: 'image/png' })),
  getProjectSink: vi.fn(async () => ({ sink: null })),
  getSinkInUse: vi.fn(async () => ({ model_name: null, inside_width: null, inside_depth: null })),
}));

const base: DepthReading = {
  countertop: 'Countertop 1 on page 3',
  field: 'CT010',
  label: 'Countertop depth',
  state: 'read',
  value: '24"',
  view: 'plan',
  page_number: 3,
  checked_by: 'printed_text',
  candidate_id: 'reading-1',
  has_picture: false,
  picture_shows_gv_mark: null,
  can_confirm: false,
  reason: null,
};
const toConfirm: DepthReading = {
  ...base,
  state: 'to_confirm',
  value: '22 7/8"',
  view: 'side',
  checked_by: 'drawn_length',
  candidate_id: 'reading-2',
  has_picture: true,
  picture_shows_gv_mark: false,
  can_confirm: true,
};
const overhang: MissingInput = {
  key: 'countertop_overhang',
  label: 'Countertop overhang',
  unit: 'in',
  where_to_find: "The architect's countertop section or the project spec.",
  scope: 'project',
  askable: true,
  sources: ['G.C / Client'],
  saved: null,
};

const waiting = (readings: DepthReading[] | undefined, inputs: MissingInput[] = [], extra: Partial<Finding> = {}): Finding =>
  ({
    id: 'f-depth',
    check_id: 'CT-DEPTH-001',
    name: 'CT-DEPTH-001',
    severity: 'FLAG',
    outcome: 'REVIEW_REQUIRED',
    reviewer_action: null,
    missing_inputs: inputs,
    depth_readings: readings,
    ...extra,
  }) as Finding;

beforeEach(() => {
  vi.mocked(confirmCandidate).mockClear();
  vi.mocked(downloadCandidateCrop).mockClear();
  vi.mocked(getProjectSink).mockResolvedValue({ sink: null });
  vi.mocked(getSinkInUse).mockReset();
  vi.mocked(getSinkInUse).mockResolvedValue({ model_name: null, inside_width: null, inside_depth: null });
});

describe('each value in plain words (#1295)', () => {
  it('says where a value was read and how code checked it', () => {
    expect(readingSentence(base)).toBe('read from the plan view: 24" (two readers, confirmed by the printed text)');
    expect(readingSentence({ ...base, view: 'section', checked_by: 'chain' })).toBe(
      'read from the section: 24" (two readers, confirmed by a chain of dimensions that adds up to it)',
    );
  });

  it('never claims a check code did not name', () => {
    expect(readingSentence({ ...base, checked_by: null })).toBe('read from the plan view: 24"');
  });

  it('asks a person to confirm a value only its drawn length checks', () => {
    expect(readingSentence(toConfirm)).toBe('read 22 7/8" on the side view — please confirm (only the drawn length checks it)');
  });

  it('says a person confirmed it, or said it is something else', () => {
    expect(readingSentence({ ...base, state: 'confirmed' })).toBe('24" from the plan view, confirmed by a person');
    expect(readingSentence({ ...base, state: 'not_this', view: 'front' })).toBe(
      'a person said the 24" on the front view is not the countertop depth',
    );
  });

  it('gives the plain reason a value was not used or not read', () => {
    expect(readingSentence({ ...base, state: 'held', value: null, reason: 'the label is cut by the edge.' })).toBe(
      'not used: the label is cut by the edge.',
    );
    expect(
      readingSentence({ ...base, state: 'held', reason: 'the view it was read on could not be tied to one countertop: why.' }),
    ).toBe('read 24" on the plan view, not used: the view it was read on could not be tied to one countertop: why.');
    expect(readingSentence({ ...base, state: 'not_read', value: null, view: null, reason: 'no plan or section view of its drawing was found.' })).toBe(
      'not read: no plan or section view of its drawing was found.',
    );
  });

  it('never shows a code word on screen', () => {
    for (const state of ['read', 'to_confirm', 'confirmed', 'not_this', 'held', 'not_read'] as const) {
      expect(readingSentence({ ...base, state, reason: 'synthetic.' })).not.toMatch(/SECOND_READER|CT0\d\d|state:|vector-text|drawn-length/);
    }
  });
});

describe('the waiting card (#1295)', () => {
  it('lists what was read per countertop, with the picture; a read value has no Confirm action', async () => {
    render(<NeedsValues finding={waiting([{ ...base, has_picture: true }])} packageId="package-1" onSaved={() => {}} />);

    const group = screen.getByRole('region', { name: 'What was read for Countertop 1 on page 3' });
    expect(group.textContent).toContain('Countertop depth: read from the plan view: 24" (two readers, confirmed by the printed text)');
    expect(await screen.findByAltText('Picture of the countertop depth read on page 3')).toBeTruthy();
    expect(downloadCandidateCrop).toHaveBeenCalledWith('project-1', 'package-1', 'reading-1');
    expect(screen.queryByRole('button', { name: 'Confirm this reading' })).toBeNull();
  });

  it('"Confirm this reading" uses the existing confirm path with the reading and its code, then asks for a run', async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const onRunChecks = vi.fn();
    render(<NeedsValues finding={waiting([toConfirm])} packageId="package-1" onSaved={onSaved} onRunChecks={onRunChecks} />);

    expect(screen.getByText(/please confirm \(only the drawn length checks it\)/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Confirm this reading' }));

    expect(confirmCandidate).toHaveBeenCalledWith('project-1', 'package-1', 'reading-2', 'CT010');
    expect(await screen.findByText('Confirmed. Run checks to use it.')).toBeTruthy();
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Confirm this reading' })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Run checks' }));
    expect(onRunChecks).toHaveBeenCalledTimes(1);
  });

  it('a refused confirmation says why and keeps the action', async () => {
    const user = userEvent.setup();
    vi.mocked(confirmCandidate).mockRejectedValueOnce(new Error('this reading has already been confirmed'));
    render(<NeedsValues finding={waiting([toConfirm])} packageId="package-1" onSaved={() => {}} />);

    await user.click(screen.getByRole('button', { name: 'Confirm this reading' }));

    expect((await screen.findByRole('alert')).textContent).toBe('this reading has already been confirmed');
    expect(screen.getByRole('button', { name: 'Confirm this reading' })).toBeTruthy();
  });

  it('a reading without its picture cannot be confirmed here, and says so', () => {
    render(<NeedsValues finding={waiting([{ ...toConfirm, has_picture: false, can_confirm: false }])} packageId="package-1" onSaved={() => {}} />);

    expect(screen.queryByRole('button', { name: 'Confirm this reading' })).toBeNull();
    expect(screen.getByText(/Its picture is not ready yet, so it cannot be confirmed here/)).toBeTruthy();
  });

  it('a held value and a value not read give their reasons, with no action', () => {
    const held = { ...base, field: 'CT012', label: 'Sink cut-out width', state: 'held' as const, value: null, reason: 'two sink outlines and no word naming the cut-out.' };
    const notRead = { ...base, field: 'CT008', label: 'Sink cut-out front-to-back size', state: 'not_read' as const, value: null, view: null, candidate_id: null, reason: 'no plan or section view of its drawing was found.' };
    render(<NeedsValues finding={waiting([held, notRead])} packageId="package-1" onSaved={() => {}} />);

    expect(screen.getByText(/Sink cut-out width:/).parentElement?.textContent).toBe(
      'Sink cut-out width: not used: two sink outlines and no word naming the cut-out.',
    );
    expect(screen.getByText(/Sink cut-out front-to-back size:/).parentElement?.textContent).toBe(
      'Sink cut-out front-to-back size: not read: no plan or section view of its drawing was found.',
    );
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('groups the lines by countertop on a whole-package result', () => {
    render(
      <NeedsValues
        finding={waiting([base, { ...base, countertop: 'Countertop 1 on page 7', candidate_id: 'reading-3', value: '25"' }])}
        packageId="package-1"
        onSaved={() => {}}
      />,
    );

    expect(screen.getByRole('region', { name: 'What was read for Countertop 1 on page 3' }).textContent).toContain('24"');
    expect(screen.getByRole('region', { name: 'What was read for Countertop 1 on page 7' }).textContent).toContain('25"');
  });

  it('shows the readings beside the settings the check still needs', () => {
    render(<NeedsValues finding={waiting([base], [overhang], { outcome: 'NOT_FOUND' })} packageId="package-1" onSaved={() => {}} />);

    const card = screen.getByRole('region', { name: 'Values this check needs' });
    expect(card.textContent).toContain('Countertop depth: read from the plan view');
    expect(screen.getByLabelText('Countertop overhang')).toBeTruthy();
  });

  it('on a closed review the lines show and nothing can be confirmed', () => {
    render(
      <SignedOffContext.Provider value={true}>
        <NeedsValues finding={waiting([toConfirm])} packageId="package-1" onSaved={() => {}} />
      </SignedOffContext.Provider>,
    );

    expect(screen.getByText(/please confirm/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Confirm this reading' })).toBeNull();
    expect(screen.getByText(SIGNED_OFF_NOTE)).toBeTruthy();
  });

  it('Other checks opens the card for a check that has readings and nothing else to ask', async () => {
    const user = userEvent.setup();
    render(
      <OtherChecks
        findings={[waiting([toConfirm])]}
        ruleNames={new Map([['CT-DEPTH-001', 'Countertop depth verification']])}
        blocking={new Set(['f-depth'])}
        onDecide={() => {}}
        onBulkDismiss={async () => ({ saved: 0, failed: [] })}
        packageId="package-1"
        onValuesSaved={() => {}}
        onRunChecks={() => {}}
      />,
    );
    await user.click(screen.getByRole('button', { name: /Other checks/ }));

    expect(await screen.findByRole('button', { name: 'Confirm this reading' })).toBeTruthy();
  });
});

describe('without readings, every screen is as before (#1295)', () => {
  it('the card is the same with no readings field and with an empty list', () => {
    const before = render(<NeedsValues finding={waiting(undefined, [overhang])} packageId="package-1" onSaved={() => {}} />);
    // React's own generated ids differ between two renders; everything else must be identical.
    const plain = (html: string) => html.replace(/_r_[0-9a-z]+_/g, 'ID');
    const html = plain(before.container.innerHTML);
    before.unmount();
    const after = render(<NeedsValues finding={waiting([], [overhang])} packageId="package-1" onSaved={() => {}} />);

    expect(plain(after.container.innerHTML)).toBe(html);
    expect(after.container.querySelector('[data-slot="depth-readings"]')).toBeNull();
  });

  it('a check with nothing to ask and nothing read shows no card', () => {
    const { container } = render(<NeedsValues finding={waiting([], [])} packageId="package-1" onSaved={() => {}} />);
    expect(container.innerHTML).toBe('');
  });

  it('no chips without tied views, and no sink note without one', async () => {
    const { container } = render(<LinkedViewChips views={[]} />);
    expect(container.innerHTML).toBe('');

    render(<SinkEntryCard packageId="package-1" />);
    expect(await screen.findByText('In use for this package: no sink size yet. The sink checks wait.')).toBeTruthy();
    expect(document.querySelector('[data-slot="vendor-sink-notes"]')).toBeNull();
  });
});

describe('the Measure page and the sink card (#1295)', () => {
  it('each tied view gets a chip with its kind and page', () => {
    render(
      <LinkedViewChips
        views={[
          { view: 'plan', page_number: 3, heading: 'TOP VIEW', by_person: false },
          { view: 'front', page_number: 3, heading: null, by_person: true },
        ]}
      />,
    );

    const chips = document.querySelectorAll('[data-slot="linked-views"] [data-slot="badge"]');
    expect([...chips].map((chip) => chip.textContent)).toEqual(['Plan view · page 3', 'Front view · page 3']);
    expect(chips[0].getAttribute('title')).toBe('Headed “TOP VIEW”. Tied by code and both AI readers.');
    expect(chips[1].getAttribute('title')).toBe('Tied by a person.');
  });

  it("the vendor's sink note is a hint and is never filled into the boxes", async () => {
    const user = userEvent.setup();
    vi.mocked(getSinkInUse).mockResolvedValue({
      model_name: null,
      inside_width: null,
      inside_depth: null,
      vendor_notes: [{ text: 'ACME-100 undermount sink', page_number: 4 }],
    });
    render(<SinkEntryCard packageId="package-1" />);

    const hint = await waitFor(() => {
      const found = document.querySelector('[data-slot="vendor-sink-notes"]');
      if (!found) throw new Error('no note yet');
      return found as HTMLElement;
    });
    expect(within(hint).getByText(sinkNoteSentence({ text: 'ACME-100 undermount sink', page_number: 4 }))).toBeTruthy();
    expect(hint.textContent).toContain("The vendor's drawing names this sink: “ACME-100 undermount sink” (page 4).");
    expect(hint.textContent).toContain("A hint only: type the sink's inside size from its spec sheet.");

    await user.click(screen.getByRole('button', { name: 'Enter the sink' }));
    expect((screen.getByLabelText('Sink name or model') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText(/Inside width/) as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText(/Inside depth/) as HTMLInputElement).value).toBe('');
  });
});
