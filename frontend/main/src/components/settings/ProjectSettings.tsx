/**
 * Company settings → Projects (#1341): each project's own values, set once here so a review never
 * has to ask for a setting.
 *
 * Per project: every setting its checks read, what it is for, which checks use it, the value in use
 * and where that comes from (this project, GV's standard, or GV's checklist default in the rulebook),
 * a box for a new value, and the project's sink card. Reviewers and admins in the project may save;
 * GV's standards above stay admin-only (Anant, 2026-10-15). Settings no drawing set of the job is
 * checked with (the cabinet width limits on a countertop-only job) sit apart, still settable.
 */
import { useEffect, useId, useState } from 'react';

import {
  ApiError,
  getProjectSettings,
  saveProjectSettings,
  type ProjectSetting,
  type ProjectSettings as Project,
  type ProjectValueIn,
} from '@/api/client';
import { SinkEntryForm, SinkSummary } from '@/components/measure/SinkEntry';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

const ORIGIN_WORD = {
  project: 'This project',
  company: 'GV standard',
  rulebook: "GV's checklist default",
  none: 'Not set',
} as const;

function originOf(setting: ProjectSetting): keyof typeof ORIGIN_WORD {
  return setting.in_use_from ?? 'none';
}

/** Where the value in use comes from, in words, with who set a project value. */
function InUse({ setting }: { setting: ProjectSetting }) {
  const origin = originOf(setting);
  return (
    <div className="flex flex-col gap-0.5">
      <span className="flex flex-wrap items-center gap-2">
        <span className="num whitespace-nowrap">{setting.in_use ?? '—'}</span>
        <span
          className={cn(
            'inline-flex w-fit items-center rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
            origin === 'project' && 'border-foreground/60 text-foreground',
            origin === 'none' && 'border-dashed border-muted-foreground/60 text-muted-foreground',
            (origin === 'company' || origin === 'rulebook') && 'border-border text-muted-foreground',
          )}
        >
          {ORIGIN_WORD[origin]}
        </span>
      </span>
      {origin === 'none' && <span className="text-xs text-muted-foreground">Its checks wait until it is set.</span>}
      {setting.project_value && (
        <span className="text-xs text-muted-foreground">
          Set by {setting.project_value.set_by} ({setting.project_value.source})
        </span>
      )}
      {origin === 'project' && setting.company_value && (
        <span className="text-xs text-muted-foreground">GV standard: <span className="num">{setting.company_value}</span></span>
      )}
      {origin !== 'rulebook' && setting.rulebook_default && (
        <span className="text-xs text-muted-foreground">Checklist default: <span className="num">{setting.rulebook_default}</span></span>
      )}
    </div>
  );
}

interface Draft {
  value: string;
  source: string;
}

function SettingRows({
  settings,
  drafts,
  saving,
  onDraft,
}: {
  settings: readonly ProjectSetting[];
  drafts: Record<string, Draft>;
  saving: boolean;
  onDraft: (name: string, draft: Draft) => void;
}) {
  return (
    <>
      {settings.map((setting) => {
        const draft = drafts[setting.name] ?? { value: '', source: '' };
        return (
          <tr key={setting.name} className="border-b align-top last:border-b-0" data-source={originOf(setting)}>
            <th scope="row" className="min-w-48 px-3 py-2 text-left font-normal">
              <span className="font-medium">{setting.label}</span>
              {setting.purpose && <span className="block text-xs text-muted-foreground">{setting.purpose}</span>}
              <span className="mt-1 block text-xs text-muted-foreground" data-part="checks">
                Used by: {setting.checks.map((check) => check.name).join(' · ')}
              </span>
            </th>
            <td className="px-3 py-2"><InUse setting={setting} /></td>
            <td className="px-3 py-2">
              <div className="flex flex-col gap-1.5">
                <Input
                  type="text"
                  className="num h-8 w-32"
                  placeholder={'e.g. 1 1/2"'}
                  aria-label={`New value for ${setting.label}`}
                  value={draft.value}
                  disabled={saving}
                  onChange={(event) => onDraft(setting.name, { ...draft, value: event.target.value })}
                />
                {setting.sources.length > 1 && (
                  <select
                    className="h-8 w-48 rounded-md border bg-background px-2 text-sm"
                    aria-label={`Where ${setting.label} comes from`}
                    value={draft.source}
                    disabled={saving}
                    onChange={(event) => onDraft(setting.name, { ...draft, source: event.target.value })}
                  >
                    <option value="">Where is it from?</option>
                    {setting.sources.map((source) => (
                      <option key={source.value} value={source.value} title={source.guidance}>{source.value}</option>
                    ))}
                  </select>
                )}
              </div>
            </td>
          </tr>
        );
      })}
    </>
  );
}

function SettingsTable({ label, settings, drafts, saving, onDraft }: {
  label: string;
  settings: readonly ProjectSetting[];
  drafts: Record<string, Draft>;
  saving: boolean;
  onDraft: (name: string, draft: Draft) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm" aria-label={label}>
        <thead className="bg-muted/50 text-left">
          <tr className="border-b">
            <th scope="col" className="px-3 py-2 font-medium">Setting · what it is for</th>
            <th scope="col" className="px-3 py-2 font-medium">In use · from</th>
            <th scope="col" className="px-3 py-2 font-medium">New value</th>
          </tr>
        </thead>
        <tbody>
          <SettingRows settings={settings} drafts={drafts} saving={saving} onDraft={onDraft} />
        </tbody>
      </table>
    </div>
  );
}

/** The values to send: only typed ones, trimmed; a source only where the person picked one. */
function projectChanges(drafts: Record<string, Draft>): ProjectValueIn[] {
  return Object.entries(drafts)
    .filter(([, draft]) => draft.value.trim() !== '')
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, draft]) => ({
      name,
      value: draft.value.trim(),
      ...(draft.source ? { source: draft.source as ProjectValueIn['source'] } : {}),
    }));
}

function ProjectBlock({ initial }: { initial: Project }) {
  const titleId = useId();
  const [project, setProject] = useState(initial);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [editingSink, setEditingSink] = useState(false);

  const changes = projectChanges(drafts);
  const needsSource = changes
    .map((change) => project.settings.find((setting) => setting.name === change.name))
    .filter((setting): setting is ProjectSetting => setting !== undefined && setting.sources.length > 1 && !drafts[setting.name]?.source);
  const used = project.settings.filter((setting) => setting.applies);
  const apart = project.settings.filter((setting) => !setting.applies);
  const onDraft = (name: string, draft: Draft) => {
    setSaved(false);
    setDrafts((prior) => ({ ...prior, [name]: draft }));
  };

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      setProject(await saveProjectSettings(project.id, changes));
      setDrafts({});
      setSaved(true);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 404
          ? 'Only a reviewer or an admin in this project can change its values.'
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-3 rounded-xl border bg-card p-4" data-project={project.id}>
      <div className="flex flex-col gap-1">
        <h3 id={titleId} className="text-base font-semibold">{project.name}</h3>
        <p className="text-sm text-muted-foreground">
          {project.product_types.length > 0 ? `Drawing sets for: ${project.product_types.join(', ')}. ` : ''}
          Reviewers and admins in this project can change these. A value typed for one package on Measurements still wins for that package.
        </p>
      </div>

      <div className="flex flex-col gap-2 rounded-lg border p-3" data-part="sink">
        <p className="text-sm font-medium">Sink</p>
        <p className="text-xs text-muted-foreground">From the sink&apos;s spec sheet. Every sink check of every package in this job uses it.</p>
        {editingSink ? (
          <SinkEntryForm
            forProject={project.id}
            onCancel={() => setEditingSink(false)}
            onSaved={(next) => {
              setEditingSink(false);
              setProject((prior) => ({ ...prior, sink: next.sink }));
            }}
          />
        ) : (
          <>
            {project.sink ? <SinkSummary sink={project.sink} /> : <p className="text-sm">No sink entered for this job yet.</p>}
            <div>
              <Button size="sm" variant="outline" onClick={() => setEditingSink(true)}>
                {project.sink ? 'Change the sink' : 'Enter the sink'}
              </Button>
            </div>
          </>
        )}
      </div>

      <SettingsTable label={`${project.name} settings`} settings={used} drafts={drafts} saving={saving} onDraft={onDraft} />
      {apart.length > 0 && (
        <details className="rounded-lg border p-3">
          <summary className="cursor-pointer text-sm">
            Not used by this project&apos;s drawing sets ({apart.length})
          </summary>
          <p className="my-2 text-xs text-muted-foreground">
            No drawing set of this job is checked with these yet (for example the cabinet width limits on a countertop-only job). They can still be set.
          </p>
          <SettingsTable label={`${project.name} settings not used yet`} settings={apart} drafts={drafts} saving={saving} onDraft={onDraft} />
        </details>
      )}

      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" disabled={saving || changes.length === 0 || needsSource.length > 0} onClick={() => void save()}>
          {saving ? 'Saving…' : changes.length === 0 ? 'No changes to save' : `Save ${changes.length} change${changes.length === 1 ? '' : 's'}`}
        </Button>
        {needsSource.length > 0 && (
          <span className="text-xs text-muted-foreground">Say where {needsSource.map((setting) => setting.label).join(', ')} comes from to save.</span>
        )}
        {saved && <span className="text-sm" role="status">Saved. Run the checks again to use these values.</span>}
      </div>
    </section>
  );
}

/** The Projects section of Company settings: one block per project you belong to. */
export function ProjectSettingsSection() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    getProjectSettings()
      .then((result) => { if (live) setProjects(result.projects); })
      .catch((caught: unknown) => { if (live) setError(caught instanceof Error ? caught.message : String(caught)); });
    return () => { live = false; };
  }, [attempt]);

  return (
    <section data-tw className="flex flex-col gap-3 font-sans" aria-labelledby="project-settings-title">
      <div className="flex flex-col gap-1">
        <h2 id="project-settings-title" className="text-base font-semibold">Projects</h2>
        <p className="text-sm text-muted-foreground">
          Each job&apos;s own values: its sink, its cabinet width limits and any number that differs from GV&apos;s standard. Set here once; a review never asks for them.
        </p>
      </div>
      {error && projects === null && (
        <p className="text-sm">
          Projects could not be loaded: {error}{' '}
          <Button size="sm" variant="ghost" onClick={() => { setError(null); setAttempt((n) => n + 1); }}>Retry</Button>
        </p>
      )}
      {projects === null && !error && <p className="text-sm text-muted-foreground" role="status">Loading projects…</p>}
      {projects !== null && projects.length === 0 && <p className="text-sm text-muted-foreground">You are not in any project yet.</p>}
      {projects?.map((project) => <ProjectBlock key={project.id} initial={project} />)}
    </section>
  );
}
