/**
 * Company settings: GV's standard numbers, set once and used on every project (#812), and each
 * project's own values under Projects (#1341), so a review never has to ask for a setting.
 *
 * Many checks need numbers that are on no drawing — the usual cabinet depth, a side panel's
 * thickness, the sink clearance, the filler limits. They are GV's house rules, the same on every job,
 * so they belong here rather than being typed again for each project. Anyone may look; only an admin
 * can save, because a standard changes every future check on every project. A project's own values
 * (its sink, cabinet width limits, overrides) are saved by its reviewers in the Projects section.
 */

import { useEffect, useState } from 'react';

import {
  ApiError,
  getCompanySettings,
  saveCompanySettings,
  type CompanySettings,
} from '../api/client';
import { CompanySettingsList } from '../components/settings/CompanySettingsList';
import { ProjectSettingsSection } from '../components/settings/ProjectSettings';
import { changedValues } from '../components/settings/companySettings';
import { PageFrame, PageLoadError } from '@/components/ui/PageFrame';
import { Button } from '@/components/ui/button';
import { InfoTip } from '@/components/ui/info-tip';

export function CompanySettingsPage() {
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
      <div className="flex flex-col gap-3">
      {error && current !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      {current === null ? (
        !error && <p className="text-sm text-muted-foreground" role="status">Loading company settings…</p>
      ) : current.settings.length === 0 ? (
        <p className="text-sm text-muted-foreground">No company settings are available. No standards are listed to edit.</p>
      ) : (
        <>
          <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
            Set once here, these apply to every project. Only an admin can change GV&apos;s standards.
            <InfoTip label="About company settings">
              <p>These are GV&apos;s standards only. Any of them can still be set differently for one project, under Projects below.</p>
              <p>Overhang, backsplash, side panel and cabinet depth are project defaults: empty until GV sets them, and a value set for a project wins. The cabinet width limits and the sink are entered per project, under Projects below.</p>
              <p>A check whose number is set nowhere says &ldquo;not found&rdquo; rather than guessing. Only an admin can save.</p>
            </InfoTip>
          </p>
          <CompanySettingsList
            settings={current.settings}
            drafts={drafts}
            saving={saving}
            onDraft={(name, value) => {
              setSaved(false);
              setDrafts((prior) => ({ ...prior, [name]: value }));
            }}
          />
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" disabled={saving || changes.length === 0} onClick={() => void save()}>
              {saving ? 'Saving…' : changes.length === 0 ? 'No changes to save' : `Save ${changes.length} change${changes.length === 1 ? '' : 's'}`}
            </Button>
            {saved && <span className="text-sm" role="status">Saved. Every project now starts from these numbers.</span>}
          </div>
        </>
      )}
      <ProjectSettingsSection />
      </div>
    </PageFrame>
  );
}
