import {
  inUseSentence,
  settingLabel,
  type CompanySetting,
} from './companySettings.js';

/**
 * GV's standard numbers, one row each (#812).
 *
 * Each row says what the number is, which checks use it, what the rulebook would use, and what is in
 * use now and where it came from — so an admin can see at a glance which standards GV still has to
 * give. Typing a new value and saving replaces that one; a blank box leaves it as it is.
 */
export function CompanySettingsList({
  settings,
  drafts,
  saving,
  onDraft,
}: {
  settings: readonly CompanySetting[];
  drafts: Record<string, string>;
  saving: boolean;
  onDraft: (name: string, value: string) => void;
}) {
  const unset = settings.filter((setting) => setting.in_use == null).length;
  return (
    <section className="company-settings" aria-labelledby="company-settings-title">
      <h2 id="company-settings-title">GV&apos;s standard numbers</h2>
      <p className="company-settings__hint">
        Set once here, these apply to every project. A project can still use its own number for one
        job. A check whose number is set nowhere says &quot;not found&quot; rather than guessing.{' '}
        <strong>
          {unset === 0 ? 'Every number is set.' : `${unset} not set yet.`}
        </strong>
      </p>
      <ul className="company-settings__list">
        {settings.map((setting) => (
          <li
            className="company-settings__item"
            key={setting.name}
            data-source={setting.in_use_from ?? 'none'}
          >
            <div className="company-settings__facts">
              <label className="company-settings__label" htmlFor={`company-${setting.name}`}>
                {settingLabel(setting.name)} <code>{setting.name}</code>
              </label>
              <span className="company-settings__in-use">{inUseSentence(setting)}</span>
              {setting.rulebook_default && setting.in_use_from !== 'rulebook' && (
                <span className="company-settings__default">
                  Rulebook default: {setting.rulebook_default}
                </span>
              )}
              {setting.rulebook_note && setting.in_use_from === 'rulebook' && (
                <span className="company-settings__note">{setting.rulebook_note}</span>
              )}
              <span className="company-settings__users">
                Used by {setting.rule_ids.join(', ')}
              </span>
            </div>
            <input
              id={`company-${setting.name}`}
              className="value-input company-settings__input"
              type="text"
              placeholder={'e.g. 24"'}
              value={drafts[setting.name] ?? ''}
              disabled={saving}
              onChange={(event) => onDraft(setting.name, event.target.value)}
            />
          </li>
        ))}
      </ul>
    </section>
  );
}
