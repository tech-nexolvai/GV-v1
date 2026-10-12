// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Finding } from '@/data/types';
import { NeedsValues } from '@/components/results/needs-values';
import { OtherChecks } from '@/components/results/other-checks';
import { SinkEntryCard } from '@/components/measure/SinkEntry';
import { InForceNote } from '@/components/measure/InForceNote';
import { drawingNeeded, saveState, savedSinceResult, typeable, type MissingInput } from '@/lib/waiting-values';
import { enterMeasurements, getProjectSink, getSinkInUse, saveProjectSink } from '@/api/client';

vi.mock('@/api/config', () => ({ projectId: () => 'project-1' }));
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  enterMeasurements: vi.fn(async () => ({})),
  getProjectSink: vi.fn(async () => ({ sink: null })),
  getSinkInUse: vi.fn(async () => ({ model_name: null, inside_width: null, inside_depth: null })),
  saveProjectSink: vi.fn(async () => ({ sink: null })),
}));

// Synthetic data only (#1106): nothing here comes from a client drawing.
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
const drawnDepth: MissingInput = {
  key: 'SHOP:CT010',
  label: 'Countertop depth, as drawn',
  unit: 'in',
  where_to_find: "The vendor's plan view or section.",
  scope: 'drawing',
  askable: false,
  sources: [],
  saved: null,
};
const sinkWidth: MissingInput = {
  key: 'sink_interior_width',
  label: 'Sink inside width',
  unit: 'in',
  where_to_find: "The sink's spec sheet.",
  scope: 'sink',
  askable: true,
  sources: [],
  saved: null,
};
const saved = { text: '1 1/4"', set_by: 'Synthetic Reviewer', set_at: '2026-10-11T10:00:00Z', source: 'G.C / Client', layer: 'project' as const };
const gvDefault = { ...saved, text: '1"', set_by: 'Synthetic Admin', layer: 'company' as const };

const waiting = (inputs: MissingInput[], extra: Partial<Finding> = {}): Finding =>
  ({
    id: 'f-depth',
    check_id: 'CT-DEPTH-001',
    name: 'CT-DEPTH-001',
    severity: 'FLAG',
    outcome: 'NOT_FOUND',
    reviewer_action: null,
    missing_inputs: inputs,
    ...extra,
  }) as Finding;

beforeEach(() => {
  vi.mocked(enterMeasurements).mockClear();
  vi.mocked(saveProjectSink).mockClear();
  vi.mocked(getProjectSink).mockReset();
  vi.mocked(getProjectSink).mockResolvedValue({ sink: null });
  vi.mocked(getSinkInUse).mockReset();
  vi.mocked(getSinkInUse).mockResolvedValue({ model_name: null, inside_width: null, inside_depth: null });
});

describe('what one Save sends (#1181)', () => {
  it('a setting goes as a value for this job with its source and note; a drawn number is never sent', () => {
    const { entry } = saveState([overhang, drawnDepth], { countertop_overhang: ' 1 1/4" ', 'SHOP:CT010': '21"' }, ' synthetic section ');

    expect(entry).toEqual({
      parameters: [{ name: 'countertop_overhang', value: '1 1/4"', scope: 'project', source: 'G.C / Client', reference: 'synthetic section' }],
    });
  });

  it('says why Save cannot be pressed: nothing typed, or no source chosen', () => {
    const twoSources = { ...overhang, key: 'bound', label: 'Bound', sources: ['Company standard', 'G.C / Client'] };

    expect(saveState([overhang], {}, '')).toEqual({ entry: null, blocked: 'Type a value, with its unit, to save.' });
    expect(saveState([twoSources], { bound: '9"' }, '')).toEqual({ entry: null, blocked: 'Choose where bound came from.' });
    expect(saveState([twoSources], { bound: '9"' }, '', { bound: 'G.C / Client' }).entry?.parameters[0].source).toBe('G.C / Client');
  });

  it('boxes: unsaved settings and settings GV\'s default answers; never the sink, a drawn number or a saved value', () => {
    const inputs = [{ ...overhang, saved }, { ...overhang, key: 'by_default', saved: gvDefault }, drawnDepth, sinkWidth];

    expect(typeable(inputs).map((input) => input.key)).toEqual(['by_default']);
    expect(drawingNeeded(inputs).map((input) => input.key)).toEqual(['SHOP:CT010']);
    expect(savedSinceResult(inputs)).toBe(true);
    expect(savedSinceResult([drawnDepth])).toBe(false);
  });
});

describe('the "Needs" line (#1181)', () => {
  it('says what, the unit and where; saves through the values endpoint; then asks for a run', async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const onRunChecks = vi.fn();
    render(<NeedsValues finding={waiting([overhang])} packageId="package-1" onSaved={onSaved} onRunChecks={onRunChecks} />);

    const section = screen.getByRole('region', { name: 'Values this check needs' });
    expect(section.textContent).toContain("Needs: Countertop overhang (in) — The architect's countertop section or the project spec.");
    const box = screen.getByLabelText('Countertop overhang') as HTMLInputElement;
    expect(box.placeholder).toBe('in');
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(screen.getByText('Type a value, with its unit, to save.')).toBeTruthy();
    expect(screen.queryByText('Saved. Run checks to update this result.')).toBeNull();

    await user.type(box, '1 1/4"');
    await user.type(screen.getByLabelText(/Source note/), 'synthetic section');
    await user.click(save);

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(enterMeasurements).toHaveBeenCalledWith('project-1', 'package-1', {
      parameters: [{ name: 'countertop_overhang', value: '1 1/4"', scope: 'project', source: 'G.C / Client', reference: 'synthetic section' }],
    });
    expect(screen.getByText('Saved. Run checks to update this result.')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Run checks' }));
    expect(onRunChecks).toHaveBeenCalled();
  });

  it('a drawn number has no box and says the check needs the drawing', () => {
    render(<NeedsValues finding={waiting([drawnDepth])} packageId="package-1" onSaved={() => {}} />);

    const section = screen.getByRole('region', { name: 'Values this check needs' });
    expect(section.textContent).toContain('Needs: Countertop depth, as drawn (in) from the section drawing (not supplied)');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
  });

  it('a value saved since the result shows where it is from, with no box, and the run is asked for', () => {
    render(<NeedsValues finding={waiting([{ ...overhang, saved }])} packageId="package-1" onSaved={() => {}} />);

    expect(screen.getByText('1 1/4"').closest('[data-slot="needs-saved"]')?.textContent).toContain('this job · Synthetic Reviewer');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Saved. Run checks to update this result.')).toBeTruthy();
  });

  it("a setting GV's default answers can still be set for this job", async () => {
    const user = userEvent.setup();
    render(<NeedsValues finding={waiting([{ ...overhang, saved: gvDefault }])} packageId="package-1" onSaved={() => {}} />);

    expect(screen.getByText('1"').closest('[data-slot="needs-saved"]')?.textContent).toContain('GV default');
    await user.type(screen.getByLabelText("Countertop overhang for this job, over GV's default"), '1 1/2"');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(enterMeasurements).toHaveBeenCalledWith('project-1', 'package-1', {
        parameters: [{ name: 'countertop_overhang', value: '1 1/2"', scope: 'project', source: 'G.C / Client' }],
      }),
    );
  });

  it('a sink value opens the sink entry, which warns it applies to every package and shows what is in use', async () => {
    const user = userEvent.setup();
    vi.mocked(getSinkInUse).mockResolvedValue({
      model_name: 'Synthetic Sink',
      inside_width: { text: '31"', origin: 'package', set_by: 'Synthetic Reviewer', set_at: null },
      inside_depth: { text: '15"', origin: 'sink_card', set_by: 'Synthetic Reviewer', set_at: null },
    });
    render(<NeedsValues finding={waiting([sinkWidth])} packageId="package-1" onSaved={() => {}} />);

    expect(screen.queryByRole('textbox')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Enter the sink…' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/This sink applies to every package in this job/)).toBeTruthy();
    const inUse = await within(dialog).findByText('In use for this package:');
    expect(inUse.closest('[data-slot="sink-in-use"]')?.textContent).toBe(
      'In use for this package: Synthetic Sink, 31" wide (typed for this package) × 15" deep (from the sink card)',
    );
    expect(within(dialog).queryByLabelText(/Front offset/)).toBeNull();
  });

  it('a sink size in use says where it is from', () => {
    render(<NeedsValues finding={waiting([{ ...sinkWidth, saved: { ...saved, text: '30"' } }])} packageId="package-1" onSaved={() => {}} />);

    expect(screen.getByText('In use for this package:').closest('p')?.textContent).toBe(
      'In use for this package: Sink inside width 30" (from the sink card)',
    );
  });

  it('nothing missing, nothing shown', () => {
    const { container } = render(<NeedsValues finding={waiting([])} packageId="package-1" onSaved={() => {}} />);
    expect(container.textContent).toBe('');
  });

  it('Other checks asks in place for a waiting check that needs you', async () => {
    const user = userEvent.setup();
    render(
      <OtherChecks
        findings={[waiting([overhang])]}
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

    expect(screen.getByRole('region', { name: 'Values this check needs' }).textContent).toContain('Needs: Countertop overhang (in)');
  });
});

describe('the sink card on Measurements (#1181)', () => {
  it('starts empty, says why Save waits, saves the name and sizes as typed, and shows the sink in use', async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    const sink = {
      model_name: 'Synthetic Sink',
      source_note: 'synthetic spec sheet',
      set_by: 'Synthetic Reviewer',
      set_at: '2026-10-11T10:00:00Z',
      inside_width: { text: '30"', set_by: 'Synthetic Reviewer', set_at: '2026-10-11T10:00:00Z' },
      inside_depth: { text: '15"', set_by: 'Synthetic Reviewer', set_at: '2026-10-11T10:00:00Z' },
    };
    vi.mocked(saveProjectSink).mockResolvedValue({ sink });
    render(<SinkEntryCard packageId="package-1" onSaved={onSaved} />);

    expect(await screen.findByText('In use for this package: no sink size yet. The sink checks wait.')).toBeTruthy();
    expect(screen.getByText(/No sink entered for this job yet/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Enter the sink' }));
    const name = screen.getByLabelText('Sink name or model') as HTMLInputElement;
    expect(name.value).toBe('');
    expect((screen.getByLabelText(/Inside width/) as HTMLInputElement).placeholder).toBe('in');
    expect((screen.getByRole('button', { name: 'Save the sink' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Type the sink name, the inside width, the inside depth to save.')).toBeTruthy();
    expect(screen.queryByLabelText(/Front offset/)).toBeNull();
    await user.type(name, 'Synthetic Sink');
    await user.type(screen.getByLabelText(/Inside width/), '30"');
    await user.type(screen.getByLabelText(/Inside depth/), '15"');
    await user.type(screen.getByLabelText(/Source note/), 'synthetic spec sheet');
    vi.mocked(getProjectSink).mockResolvedValue({ sink });
    vi.mocked(getSinkInUse).mockResolvedValue({
      model_name: 'Synthetic Sink',
      inside_width: { text: '30"', origin: 'sink_card', set_by: 'Synthetic Reviewer', set_at: null },
      inside_depth: { text: '15"', origin: 'sink_card', set_by: 'Synthetic Reviewer', set_at: null },
    });
    await user.click(screen.getByRole('button', { name: 'Save the sink' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saveProjectSink).toHaveBeenCalledWith('project-1', {
      model_name: 'Synthetic Sink',
      inside_width: '30"',
      inside_depth: '15"',
      source_note: 'synthetic spec sheet',
    });
    const inUse = await screen.findByText('In use for this package:');
    expect(inUse.closest('[data-slot="sink-in-use"]')?.textContent).toContain('Synthetic Sink, 30" wide (from the sink card) × 15" deep (from the sink card)');
    expect(screen.getByText('Saved. Run checks to update the sink results.')).toBeTruthy();
  });
});

describe('Measurements shows what is in force (#1181)', () => {
  it("says GV's default or the sink card instead of a blank, and says when nothing is set", () => {
    const { container, rerender } = render(<InForceNote inForce={{ text: '22"', origin: 'gv_default', set_by: 'Synthetic Admin' }} />);
    expect(container.textContent).toBe('In use: 22" (GV\'s default, Synthetic Admin)');

    rerender(<InForceNote inForce={{ text: '30"', origin: 'sink_card', set_by: 'Synthetic Reviewer' }} />);
    expect(container.textContent).toBe('In use: 30" (from the sink card, Synthetic Reviewer)');

    rerender(<InForceNote inForce={null} />);
    expect(container.textContent).toBe('In use: nothing yet — the checks that need it wait.');
  });
});

describe('company defaults and the changed values (#1181)', () => {
  it('a project default is marked as one in Company settings, with nothing in use', async () => {
    const { CompanySettingsList } = await import('@/components/settings/CompanySettingsList');
    render(
      <CompanySettingsList
        settings={[{ name: 'countertop_overhang', scope: 'project', rule_ids: ['CT-DEPTH-001'], rulebook_default: null, rulebook_note: null, in_use: null, in_use_from: null, kind: 'project_default' }]}
        drafts={{}}
        saving={false}
        onDraft={() => {}}
      />,
    );

    expect(screen.getByText('Project default: a value typed for a project wins.')).toBeTruthy();
    expect(document.querySelector('tr[data-source="none"]')).toBeTruthy();
  });

  it('values entered for the job and GV defaults used are listed on the changed-values panel', async () => {
    const { ChangedValuesPanel } = await import('@/components/output/ChangedValuesPanel');
    const line = 'Sink inside width: 30" (this job; G.C / Client: Sink Synthetic; set by Synthetic Reviewer on 12 Oct 2026)';
    const used = 'Cabinet depth: 22" (GV default; Company standard; set by Synthetic Admin on 12 Oct 2026)';
    render(
      <ChangedValuesPanel
        state="ready"
        currentRevisionId="rev"
        value={{ revision_id: 'rev', status: 'available', message: null, company_standards_displaced: [], outstanding: [], entered: [line], defaults_used: [used] }}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Entered for this job' })).toBeTruthy();
    expect(screen.getByText(line)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'GV defaults used' })).toBeTruthy();
    expect(screen.getByText(used)).toBeTruthy();
  });
});
