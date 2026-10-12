import { settingLabel, type CompanySetting } from '@/components/settings/companySettings';
import { mixedFractionsIn } from '@/lib/fractions';
import { companySourceWords, savedText, typedBy, type SavedParameter } from '@/lib/saved-values';
import { Hint, LoadError, StepSection } from './wizard-ui';

/**
 * Step 4's list of GV's company settings in use (#1183): each one's value, where that value comes
 * from (GV's own setting or the rulebook's default), and, where this project saved its own, that
 * the project's value is used instead. From `GET /company-settings`, the same list the Company
 * settings page shows; this only reads it.
 */
export function CompanySettingsInUse({
  settings,
  error,
  onRetry,
  saved,
}: {
  /** `null` while loading or after a failure. */
  settings: readonly CompanySetting[] | null;
  error: string | null;
  onRetry: () => void;
  saved: Record<string, SavedParameter>;
}) {
  return (
    <StepSection
      id="measure-company-title"
      slot="measure-company-settings"
      title="Company settings in use"
      line="A blank setting uses GV's company setting, else the rulebook's default."
    >
      {error ? (
        <LoadError onRetry={onRetry} retryLabel="Try loading company settings again">
          Company settings could not be loaded, so a blank setting&apos;s value is not shown here. {error}
        </LoadError>
      ) : settings === null ? (
        <Hint role="status">Loading company settings…</Hint>
      ) : settings.length === 0 ? (
        <Hint>The published rules use no company settings.</Hint>
      ) : (
        <ul className="divide-y rounded-xl border bg-card" aria-labelledby="measure-company-title">
          {settings.map((setting) => {
            const own = saved[setting.name];
            return (
              <li
                key={setting.name}
                className="grid gap-x-4 gap-y-1 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]"
                data-setting={setting.name}
                data-source={setting.in_use_from ?? 'none'}
              >
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-sm font-medium">{settingLabel(setting.name)}</span>
                  <span className="num break-all text-xs text-muted-foreground">{setting.name}</span>
                </div>
                <div className="flex min-w-0 flex-col gap-0.5">
                  {setting.in_use == null ? (
                    <span className="text-sm text-muted-foreground">Not set: checks that need it say &ldquo;not found&rdquo;.</span>
                  ) : (
                    <span className="text-sm">
                      <span className="num">{mixedFractionsIn(setting.in_use)}</span>{' '}
                      <span className="text-muted-foreground">· {companySourceWords(setting)}</span>
                    </span>
                  )}
                  {own && (
                    <span className="text-xs text-muted-foreground" data-part="project-override">
                      This {own.layer === 'run' ? 'review' : 'project'} uses <span className="num text-foreground">{savedText(own.value)}</span> instead ({typedBy(own.value.set_by, own.value.set_at)}).
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </StepSection>
  );
}
