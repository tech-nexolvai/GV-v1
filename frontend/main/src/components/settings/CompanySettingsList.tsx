import { Progress } from '@/components/ui/progress';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import {
  inUseSentence,
  settingCounts,
  settingDate,
  settingLabel,
  type CompanySetting,
} from './companySettings.js';

const SOURCE_WORD = { company: 'GV standard', rulebook: 'Rulebook default', none: 'Not set' } as const;

/** Where the value in use comes from, as a word with its own edge (never colour alone). */
function SourceBadge({ setting }: { setting: CompanySetting }) {
  const source = setting.in_use_from ?? 'none';
  return (
    <span
      data-source={source}
      className={cn(
        'inline-flex w-fit items-center rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        source === 'company' && 'border-foreground/60 text-foreground',
        source === 'rulebook' && 'border-border text-muted-foreground',
        source === 'none' && 'border-dashed border-muted-foreground/60 text-muted-foreground',
      )}
    >
      {SOURCE_WORD[source]}
    </span>
  );
}

/**
 * GV's standard numbers as one table (#812; table #1072).
 *
 * Each row: what the number is, the value in use and where it comes from (GV's standard, the
 * rulebook's default, or nothing), the checks that read it, and a box for a new value. A blank box
 * leaves the value as it is. Above it, how many GV has set. Every one of them can still be set
 * differently for one project, in that review's Measurements, so the table does not suggest a lock.
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
  const counts = settingCounts(settings);
  return (
    <section data-tw className="flex flex-col gap-3 font-sans" aria-labelledby="company-settings-title">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 id="company-settings-title" className="text-base font-semibold">GV&apos;s standard numbers</h2>
        {/* The bar counts what GV has set: the rulebook gives most of these a default, so "has a
            value" would be full before GV had decided anything (#1072 review). */}
        <div className="flex min-w-56 flex-col gap-1" data-part="meter">
          <p className="text-sm">
            <span className="num">{counts.byCompany}</span> of <span className="num">{counts.total}</span> set by GV
            {counts.byRulebook > 0 && <> · <span className="num">{counts.byRulebook}</span> on the rulebook&apos;s default</>}
            {counts.inUse < counts.total && (
              <> · <strong><span className="num">{counts.total - counts.inUse}</span> not set yet</strong></>
            )}
          </p>
          <Progress
            value={counts.total === 0 ? 0 : (counts.byCompany / counts.total) * 100}
            aria-label={`${counts.byCompany} of ${counts.total} set by GV`}
          />
        </div>
      </div>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm" aria-labelledby="company-settings-title">
          <thead className="bg-muted/50 text-left">
            <tr className="border-b">
              <th scope="col" className="px-3 py-2 font-medium">Setting</th>
              <th scope="col" className="px-3 py-2 font-medium">In use · new value</th>
              <th scope="col" className="hidden px-3 py-2 font-medium lg:table-cell">Used by</th>
            </tr>
          </thead>
          <tbody>
            {settings.map((setting) => (
              <tr key={setting.name} className="border-b align-top last:border-b-0" data-source={setting.in_use_from ?? 'none'}>
                <th scope="row" className="min-w-40 px-3 py-2 text-left font-normal">
                  <label className="font-medium" htmlFor={`company-${setting.name}`}>
                    {settingLabel(setting.name)}
                  </label>{' '}
                  <code className="text-xs text-muted-foreground">{setting.name}</code>
                  {setting.rulebook_note && setting.in_use_from === 'rulebook' && (
                    <p className="mt-1 max-w-md text-xs text-muted-foreground" data-part="note">{setting.rulebook_note}</p>
                  )}
                </th>
                <td className="px-3 py-2">
                  <div className="flex flex-col gap-1">
                    <span className="num" aria-hidden="true">{setting.in_use ?? '—'}</span>
                    <span aria-hidden="true"><SourceBadge setting={setting} /></span>
                    {setting.in_use_from === 'company' && setting.company_set_by && (
                      <span className="text-xs text-muted-foreground" aria-hidden="true">
                        by {setting.company_set_by}
                        {setting.company_set_at && <> on {settingDate(setting.company_set_at)}</>}
                      </span>
                    )}
                    {/* The same facts as one sentence, for a screen reader. */}
                    <span className="sr-only" id={`company-${setting.name}-now`}>{inUseSentence(setting)}</span>
                    {/* The new value sits under the one in use, so it stays in view on a phone. */}
                    <Input
                      id={`company-${setting.name}`}
                      type="text"
                      className="num mt-1 w-32"
                      placeholder={'e.g. 2 1/2"'}
                      aria-describedby={`company-${setting.name}-now`}
                      value={drafts[setting.name] ?? ''}
                      disabled={saving}
                      onChange={(event) => onDraft(setting.name, event.target.value)}
                    />
                    {setting.rulebook_default && setting.in_use_from !== 'rulebook' && (
                      <p className="text-xs text-muted-foreground">
                        Rulebook default: <span className="num">{setting.rulebook_default}</span>
                      </p>
                    )}
                  </div>
                </td>
                <td className="hidden px-3 py-2 lg:table-cell" data-part="used-by">
                  <span className="sr-only">Used by </span>
                  <span className="flex flex-wrap gap-1">
                    {setting.rule_ids.map((id) => (
                      <code key={id} className="rounded border px-1.5 py-0.5 text-xs">{id}</code>
                    ))}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
