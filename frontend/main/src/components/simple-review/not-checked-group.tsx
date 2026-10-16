import { Fragment, useRef, useState, type FormEvent } from 'react';
import { Check, ChevronRight, Loader2 } from 'lucide-react';

import type { JobValueSuggestion, NotCheckedItem, NotCheckedSetting } from '@/api/review-items';
import { formatRoute } from '@/app/route';
import { cn } from '@/lib/utils';
import { inchValue, jobValuesWaiting, whereWord } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import type { SaveError } from './failures-strip';
import { PageTag } from './words';

/**
 * The checks that never ran (the reader they need is off). Quiet and closed by default: they are not
 * the AI's mistakes, and they are printed NOT CHECKED on the cloud PDF. One click clears them all.
 * A check whose run also lacked a setting links to that field on Company settings (#1373), only
 * when the API names it: the screen never works out which setting a check needs. Saving a setting
 * does not re-run the check, so one the API says is set now (`set_now`, #1382) is not linked again:
 * the line says it was set after this check ran, and to run the checks again.
 *
 * **This job's values (#1403).** The values the open checks still wait for (as the API names them)
 * are summed up in one line, with the full list folded under "Show all N", and entered here, once,
 * in inches: they are per job (the GC's or the client's), saved on the project like Company settings
 * → Projects saves them, and the checks run again. Someone who may not save sees a read-only line.
 *
 * **What the job values reader found (#1409)** is shown beside each field: "Found 3/4" · p.55 · Look ·
 * Use". A value both AIs and code agree on (`auto`) is pre-filled and labelled "Filled from p.N by
 * two AIs and code · Change"; a plain suggestion is never pre-filled. Nothing is saved until Save.
 */
export function NotCheckedGroup({
  items,
  note,
  busy,
  error,
  onClearAll,
  open: controlledOpen,
  onOpenChange,
  canSaveJobValues = false,
  onSaveJobValues,
  suggestions = [],
  onLook,
}: {
  /** What the job values reader found, one per value (#1409). */
  suggestions?: readonly JobValueSuggestion[];
  /** Open the side-by-side viewer at a found value's page, ringed (#1409). */
  onLook?: (found: JobValueSuggestion, name: string) => void;
  /** Whether the caller may save this project's values (`can_save_job_values`, #1403). */
  canSaveJobValues?: boolean;
  /** Save this job's values (inches, with the inch mark) and run the checks again. */
  onSaveJobValues?: (values: JobValue[]) => void;
  /** Open from outside (the Assistant's "open item"); uncontrolled when left out. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  items: readonly NotCheckedItem[];
  note: string | null;
  busy: boolean;
  error: SaveError | null;
  onClearAll: () => void;
}) {
  const [ownOpen, setOwnOpen] = useState(false);
  const open = controlledOpen ?? ownOpen;
  const setOpen = (next: (value: boolean) => boolean) => {
    const value = next(open);
    setOwnOpen(value);
    onOpenChange?.(value);
  };
  if (items.length === 0) return null;
  const waiting = items.filter((item) => item.status === 'open').length;
  const job = jobValuesWaiting(items);

  return (
    <section aria-labelledby="not-checked-title" data-slot="not-checked-group" className="rounded-xl border border-dashed border-border">
      <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 sm:px-6">
        <h2 id="not-checked-title" className="min-w-0 basis-full sm:basis-auto sm:flex-1">
          <button
            type="button"
            aria-expanded={open}
            aria-controls="not-checked-list"
            onClick={() => setOpen((value) => !value)}
            className="flex items-center gap-3 rounded-md text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 text-muted-foreground transition-transform duration-150 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
            <span className="inline-flex items-center gap-1.5 text-[15px] font-medium text-outcome-missing-fg">
              <OutcomeIcon outcome="NOT_FOUND" size={14} />
              Not checked
            </span>
            <span className="num text-[13px] text-muted-foreground">{items.length}</span>
          </button>
        </h2>
        {waiting > 0 ? (
          <Button size="sm" variant="outline" onClick={onClearAll} disabled={busy} className="ml-7 sm:ml-0">
            {busy && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
            Clear all as not checked
          </Button>
        ) : (
          <span className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
            <Check aria-hidden="true" className="size-3.5" />
            Cleared as not checked
          </span>
        )}
        {job.settings.length > 0 && (
          <JobValues
            job={job}
            total={items.length}
            busy={busy}
            canSave={canSaveJobValues && onSaveJobValues !== undefined}
            onShowAll={() => setOpen(() => true)}
            onSave={(values) => onSaveJobValues?.(values)}
            suggestions={suggestions}
            onLook={onLook}
          />
        )}
        {note && <p className="basis-full pl-7 text-[13px] leading-5 text-muted-foreground">{note}</p>}
        {error && (
          <p role="alert" className="flex basis-full items-center gap-2 pl-7 text-[13px] text-outcome-fail-fg">
            Not saved: {error.message}
            <Button size="xs" variant="ghost" onClick={error.retry}>Retry</Button>
          </p>
        )}
      </div>
      <div
        className={cn('grid transition-[grid-template-rows] duration-150 ease-out motion-reduce:transition-none', open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}
      >
        <ul id="not-checked-list" hidden={!open} className="min-h-0 overflow-hidden border-t border-dashed border-border">
          {items.map((item) => (
            <li key={item.key} data-entry-key={item.key} tabIndex={-1} className="flex min-h-11 outline-none focus-visible:bg-accent/60 items-center gap-3 px-4 py-2 pl-11 sm:px-6 sm:pl-[3.25rem] text-[13px] text-muted-foreground">
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate">
                  <span className="text-foreground">{item.check}</span>
                  <span className="ml-2">{whereWord(item)}</span>
                </span>
                {item.settings && item.settings.length > 0 && <SettingLinks settings={item.settings} />}
              </span>
              <PageTag page={item.page} />
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

const LINK = 'rounded-sm text-foreground underline underline-offset-2 outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-ring';

/** `#/settings/company?focus=<key>&project=<id>`: Company settings opened on that project's field. */
function settingHref(setting: NotCheckedSetting): string {
  return formatRoute({ page: 'settings', packageId: null, focus: setting.key, project: setting.project_id });
}

/** "A", "A and B", "A, B and C". */
function names(settings: readonly NotCheckedSetting[]): string {
  const all = settings.map((setting) => setting.name);
  return all.length < 2 ? all.join('') : `${all.slice(0, -1).join(', ')} and ${all[all.length - 1]}`;
}

/**
 * What the check's run lacked. One still unset is a link to its field; one set since the run is not
 * (the field already has a value): it says so, and once none is left unset, to run the checks again.
 */
export function SettingLinks({ settings }: { settings: readonly NotCheckedSetting[] }) {
  const unset = settings.filter((setting) => !setting.set_now);
  const setSince = settings.filter((setting) => setting.set_now);
  return (
    <>
      {unset.length > 0 && <UnsetLinks settings={unset} />}
      {setSince.length > 0 && (
        <span>
          {names(setSince)} {setSince.length === 1 ? 'was' : 'were'} set after this check ran.
          {unset.length === 0 && ' Run the checks again.'}
        </span>
      )}
    </>
  );
}

/** The settings the check waits for, each a link to its field. One reads "Set it in Company settings". */
function UnsetLinks({ settings }: { settings: readonly NotCheckedSetting[] }) {
  if (settings.length === 1) {
    const [setting] = settings;
    return (
      <span className="truncate">
        Needs {setting.name}.{' '}
        <a href={settingHref(setting)} className={LINK}>Set it in Company settings</a>
      </span>
    );
  }
  return (
    <span>
      Set in Company settings:{' '}
      {settings.map((setting, index) => (
        <Fragment key={setting.key}>
          {index > 0 && ' · '}
          <a href={settingHref(setting)} aria-label={`Set ${setting.name} in Company settings`} className={LINK}>
            {setting.name}
          </a>
        </Fragment>
      ))}
    </span>
  );
}

/** One of this job's values as sent: its setting's name and the inches typed, with the inch mark. */
export interface JobValue {
  name: string;
  value: string;
}

/** A found value as the field shows it: the inches without the inch mark (the field says "in"). */
function fieldText(value: string): string {
  return value.replace(/"$/, '');
}

function JobValues({
  job,
  total,
  busy,
  canSave,
  onShowAll,
  onSave,
  suggestions,
  onLook,
}: {
  job: ReturnType<typeof jobValuesWaiting>;
  total: number;
  busy: boolean;
  canSave: boolean;
  onShowAll: () => void;
  onSave: (values: JobValue[]) => void;
  suggestions: readonly JobValueSuggestion[];
  onLook?: (found: JobValueSuggestion, name: string) => void;
}) {
  const found = new Map(suggestions.filter((one) => job.settings.some((setting) => setting.key === one.name)).map((one) => [one.name, one]));
  // Auto values are pre-filled (never saved until Save), so the form opens with them in view.
  const autos = Object.fromEntries([...found.values()].filter((one) => one.auto).map((one) => [one.name, fieldText(one.value)]));
  const [formOpen, setFormOpen] = useState(Object.keys(autos).length > 0);
  const [typed, setTyped] = useState<Record<string, string>>(autos);
  /** The fields still holding the value the reader filled in, unchanged. */
  const [filled, setFilled] = useState<Record<string, boolean>>(() => Object.fromEntries(Object.keys(autos).map((key) => [key, true])));
  const inputs = useRef<Record<string, HTMLInputElement | null>>({});
  const [problem, setProblem] = useState<string | null>(null);

  function openForm() {
    // Fields still empty take the auto values; anything typed stays.
    setTyped((all) => ({ ...autos, ...Object.fromEntries(Object.entries(all).filter(([, text]) => text.trim() !== '')) }));
    setFilled((all) => ({ ...all, ...Object.fromEntries(Object.keys(autos).filter((key) => !(typed[key] ?? '').trim() || typed[key] === autos[key]).map((key) => [key, true])) }));
    setFormOpen(true);
  }

  function use(key: string, value: string) {
    setTyped((all) => ({ ...all, [key]: fieldText(value) }));
    setFilled((all) => ({ ...all, [key]: false }));
  }

  function change(key: string) {
    setFilled((all) => ({ ...all, [key]: false }));
    inputs.current[key]?.focus();
    inputs.current[key]?.select();
  }
  const count = job.countertops ?? job.checks;
  const noun = job.countertops === null ? 'check' : 'countertop';
  const values = job.settings.length === 1 ? "this job's value" : `this job's ${job.settings.length} values`;
  const line = `${count} ${noun}${count === 1 ? ' is' : 's are'} waiting for ${values}: ${job.settings.map((setting) => setting.name).join(' · ')}`;

  function submit(event: FormEvent) {
    event.preventDefault();
    const out: JobValue[] = [];
    for (const setting of job.settings) {
      const text = (typed[setting.key] ?? '').trim();
      if (text === '') continue;
      const value = inchValue(text);
      if (value === null) {
        setProblem(`${setting.name}: type inches, e.g. 1 1/2`);
        return;
      }
      out.push({ name: setting.key, value });
    }
    if (out.length === 0) {
      setProblem('Type at least one value.');
      return;
    }
    setProblem(null);
    onSave(out);
  }

  return (
    <div className="flex basis-full flex-col gap-2 pl-7" data-part="job-values">
      <p className="text-[13px] leading-5 text-foreground">
        <span data-testid="job-values-summary">{line}</span>{' '}
        <button type="button" onClick={onShowAll} className={LINK}>
          Show all {total}
        </button>
      </p>
      {!canSave ? (
        <p className="text-[13px] text-muted-foreground">Only this project&apos;s reviewers can enter its values.</p>
      ) : !formOpen ? (
        <div>
          <Button size="sm" variant="outline" onClick={openForm}>
            Enter this job&apos;s values
          </Button>
        </div>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-2" aria-label="This job's values">
          <p className="text-[13px] text-muted-foreground">From the drawings&apos; sections or the GC&apos;s spec for this job.</p>
          <div className="grid gap-2 sm:grid-cols-2">
            {job.settings.map((setting) => {
              const one = found.get(setting.key);
              return (
                <div key={setting.key} className="flex flex-col gap-1">
                  <label className="flex items-center gap-2 text-[13px]">
                    <span className="min-w-40 text-foreground">{setting.name}</span>
                    <span className="relative">
                      <Input
                        ref={(element) => {
                          inputs.current[setting.key] = element;
                        }}
                        aria-label={setting.name}
                        inputMode="decimal"
                        autoComplete="off"
                        value={typed[setting.key] ?? ''}
                        onChange={(event) => {
                          setTyped((all) => ({ ...all, [setting.key]: event.target.value }));
                          setFilled((all) => ({ ...all, [setting.key]: false }));
                        }}
                        className="num h-8 w-28 pr-6"
                        placeholder="1 1/2"
                      />
                      <span aria-hidden="true" className="pointer-events-none absolute top-1/2 right-2 -translate-y-1/2 text-muted-foreground">
                        in
                      </span>
                    </span>
                  </label>
                  {one && (
                    <FoundLine
                      found={one}
                      filled={one.auto && (filled[setting.key] ?? false) && typed[setting.key] === fieldText(one.value)}
                      onLook={onLook ? () => onLook(one, setting.name) : undefined}
                      onUse={() => use(setting.key, one.value)}
                      onChange={() => change(setting.key)}
                    />
                  )}
                </div>
              );
            })}
          </div>
          {problem && (
            <p role="alert" className="text-[13px] text-outcome-fail-fg">
              {problem}
            </p>
          )}
          <div className="flex gap-2">
            <Button size="sm" type="submit" disabled={busy}>
              {busy && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
              Save and check again
            </Button>
            <Button size="sm" variant="ghost" type="button" onClick={() => setFormOpen(false)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}

const QUIET_LINK = 'rounded-sm underline underline-offset-2 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring';

/**
 * What the job values reader found for one field (#1409). Pre-filled (auto, unchanged): "Filled from
 * p.N by two AIs and code · Look · Change". Otherwise: "Found 3/4" · p.55 · Look · Use", never filled.
 */
export function FoundLine({
  found,
  filled,
  onLook,
  onUse,
  onChange,
}: {
  found: JobValueSuggestion;
  filled: boolean;
  onLook?: () => void;
  onUse: () => void;
  onChange: () => void;
}) {
  const by = found.source === 'drawing' ? "Read from the drawing's own text" : 'Read by two AIs';
  const look = onLook && (
    <>
      {' · '}
      <button type="button" onClick={onLook} className={QUIET_LINK}>
        Look
      </button>
    </>
  );
  if (filled) {
    return (
      <p data-part="job-value-filled" className="pl-[10.5rem] text-[12px] text-muted-foreground">
        Filled from p.{found.page} by two AIs and code{look}
        {' · '}
        <button type="button" onClick={onChange} className={QUIET_LINK}>
          Change
        </button>
      </p>
    );
  }
  return (
    <p data-part="job-value-found" title={found.note ? `${by}. ${found.note}` : by} className="pl-[10.5rem] text-[12px] text-muted-foreground">
      Found <span className="num text-foreground">{found.value}</span> · p.{found.page}
      {look}
      {' · '}
      <button type="button" onClick={onUse} aria-label={`Use ${found.value}`} className={QUIET_LINK}>
        Use
      </button>
    </p>
  );
}
