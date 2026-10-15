/**
 * Company settings: GV's standard numbers, set once and used on every project (#812), and each
 * project's own values under Projects (#1341), so a review never has to ask for a setting.
 *
 * Many checks need numbers that are on no drawing — the usual cabinet depth, a side panel's
 * thickness, the sink clearance, the filler limits. They are GV's house rules, the same on every job,
 * so they belong here rather than being typed again for each project. Anyone may look; only an admin
 * can save, because a standard changes every future check on every project. The server says whether
 * the caller may save (`can_save`, #1369); anyone else sees the values without boxes or Save. A project's own values
 * (its sink, cabinet width limits, overrides) are saved by its reviewers in the Projects section.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  ApiError,
  getCompanySettings,
  saveCompanySettings,
  type CompanySettings,
} from '../api/client';
import { CompanySettingsList } from '../components/settings/CompanySettingsList';
import { ProjectSettingsSection } from '../components/settings/ProjectSettings';
import { changedValues, focusSettingField, settingLabel, unitHint } from '../components/settings/companySettings';
import { PageFrame, PageLoadError } from '@/components/ui/PageFrame';
import { Button } from '@/components/ui/button';
import { InfoTip } from '@/components/ui/info-tip';
import { cn } from '@/lib/utils';

export function CompanySettingsPage({ focus, project }: {
  /** A setting to open the page on (#1369): `#/settings/company?focus=<setting>`, from "Not checked". */
  focus?: string;
  /** The project whose box to prefer for `focus`. */
  project?: string;
} = {}) {
  const [current, setCurrent] = useState<CompanySettings | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    getCompanySettings()
      .then((result) => {
        if (live) setCurrent(result);
      })
      .catch((caught: unknown) => {
        if (live) setError(caught instanceof ApiError ? caught.message : String(caught));
      });
    return () => {
      live = false;
    };
  }, [attempt]);

  const changes = changedValues(drafts);
  // Only what the server says (#1369): a missing answer shows no boxes rather than ones it would refuse.
  const canSave = current?.can_save === true;
  const unclear = changes.filter((change) => unitHint(change.value) !== null);

  // A link to one field (#1369): once both lists are on screen, open the page on that field. Each link
  // is followed once, so typing elsewhere afterwards never pulls the focus back.
  const [projectsSettled, setProjectsSettled] = useState(false);
  const onProjectsSettled = useCallback(() => setProjectsSettled(true), []);
  const [missing, setMissing] = useState<string | null>(null);
  const followed = useRef<string | null>(null);
  const page = useRef<HTMLDivElement>(null);
  const companySettled = current !== null || error !== null;
  useEffect(() => {
    if (!focus || !companySettled || !projectsSettled || page.current === null) return;
    const link = `${focus}|${project ?? ''}`;
    if (followed.current === link) return;
    followed.current = link;
    setMissing(focusSettingField(page.current, focus, project) ? null : focus);
  }, [focus, project, companySettled, projectsSettled]);

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      setCurrent(await saveCompanySettings(changes));
      setDrafts({});
      setSaved(true);
    } catch (caught) {
      // A refused save comes back as "Not found" on purpose (every authorisation failure does), so
      // the reason a person can act on is said here.
      // The server stays the authority: whatever it refused with is said here, as it said it.
      setError(
        caught instanceof ApiError && caught.status === 404
          ? 'Only an admin can change company settings.'
          : caught instanceof ApiError
            ? caught.message
            : String(caught),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <PageFrame title="Company settings" description={<>
          GV&apos;s standard numbers, the same on every job. Type a value with its unit, such as{' '}
          <span className="num whitespace-nowrap">24&quot;</span> or <span className="num whitespace-nowrap">610 mm</span>.
        </>}>
      {error && current === null && <PageLoadError title="Company settings could not be loaded" message={error} onRetry={() => { setError(null); setAttempt((value) => value + 1); }} />}
      <div className="flex flex-col gap-3" ref={page}>
      {missing !== null && (
        <p className="rounded-lg border border-dashed px-3 py-2 text-sm" role="status">
          The setting this link points to, &ldquo;{settingLabel(missing)}&rdquo;, is not listed here: it is not one of GV&apos;s standards or a value any of your projects&apos; checks read.
        </p>
      )}
      {current === null ? (
        !error && <p className="text-sm text-muted-foreground" role="status">Loading company settings…</p>
      ) : current.settings.length === 0 ? (
        <p className="text-sm text-muted-foreground">No company settings are available. No standards are listed to edit.</p>
      ) : (
        <>
          <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
            Set once here, these apply to every project.
            <InfoTip label="About company settings">
              <p>These are GV&apos;s standards only. Any of them can still be set differently for one project, under Projects below.</p>
              <p>Overhang, backsplash, side panel and cabinet depth are project defaults: empty until GV sets them, and a value set for a project wins. The cabinet width limits and the sink are entered per project, under Projects below.</p>
              <p>A check whose number is set nowhere says &ldquo;not found&rdquo; rather than guessing. Only an admin can save.</p>
            </InfoTip>
          </p>
          {!canSave && (
            <p className="text-sm" data-part="read-only">Only an admin can change GV standards.</p>
          )}
          <CompanySettingsList
            settings={current.settings}
            drafts={drafts}
            saving={saving}
            readOnly={!canSave}
            onDraft={(name, value) => {
              setSaved(false);
              setDrafts((prior) => ({ ...prior, [name]: value }));
            }}
          />
          {canSave && <div className="flex flex-wrap items-center gap-3">
            <Button type="button" disabled={saving || changes.length === 0 || unclear.length > 0} onClick={() => void save()}>
              {saving ? 'Saving…' : changes.length === 0 ? 'No changes to save' : `Save ${changes.length} change${changes.length === 1 ? '' : 's'}`}
            </Button>
            {/* Always in the page, so a screen reader hears what a save did (#1369): saved, or the server's refusal. */}
            <span role="status" className={cn('text-sm', error && 'text-destructive')} data-part="save-status">
              {error ?? (saved ? 'Saved. Every project now starts from these numbers.' : '')}
            </span>
          </div>}
        </>
      )}
      <ProjectSettingsSection onSettled={onProjectsSettled} />
      </div>
    </PageFrame>
  );
}
