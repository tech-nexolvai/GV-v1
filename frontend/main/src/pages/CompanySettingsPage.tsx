/**
 * Company settings: GV's standard numbers, set once and used on every project (#812).
 *
 * Many checks need numbers that are on no drawing — the usual cabinet depth, a side panel's
 * thickness, the sink clearance, the filler limits. They are GV's house rules, the same on every job,
 * so they belong here rather than being typed again for each project. Anyone may look; only an admin
 * can save, because a standard changes every future check on every project.
 */

import { useEffect, useState } from 'react';

import {
  ApiError,
  getCompanySettings,
  saveCompanySettings,
  type CompanySettings,
} from '../api/client';
import { CompanySettingsList } from '../components/settings/CompanySettingsList';
import { changedValues } from '../components/settings/companySettings';
import './CompanySettingsPage.css';

export function CompanySettingsPage() {
  const [current, setCurrent] = useState<CompanySettings | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

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
  }, []);

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
    <div className="company-settings-page">
      <header className="company-settings-page__head">
        <h1>Company settings</h1>
        <p>
          GV&apos;s house rules — the numbers that are the same on every job and written on no
          drawing. Type a value with its unit, <code>24&quot;</code> or <code>610 mm</code>, and save.
        </p>
      </header>
      {error && (
        <p className="company-settings-page__error" role="alert">
          {error}
        </p>
      )}
      {current === null ? (
        !error && <p className="company-settings-page__loading">Loading…</p>
      ) : (
        <>
          <CompanySettingsList
            settings={current.settings}
            drafts={drafts}
            saving={saving}
            onDraft={(name, value) => {
              setSaved(false);
              setDrafts((prior) => ({ ...prior, [name]: value }));
            }}
          />
          <div className="company-settings-page__actions">
            <button
              type="button"
              className="btn btn--primary"
              disabled={saving || changes.length === 0}
              onClick={() => void save()}
            >
              {saving ? 'Saving…' : `Save ${changes.length || ''} change${changes.length === 1 ? '' : 's'}`}
            </button>
            {saved && <span role="status">Saved. Every project now starts from these numbers.</span>}
          </div>
        </>
      )}
    </div>
  );
}
