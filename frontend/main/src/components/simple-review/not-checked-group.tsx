import { Fragment, useState } from 'react';
import { Check, ChevronRight, Loader2 } from 'lucide-react';

import type { NotCheckedItem, NotCheckedSetting } from '@/api/review-items';
import { formatRoute } from '@/app/route';
import { cn } from '@/lib/utils';
import { whereWord } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import type { SaveError } from './failures-strip';
import { PageTag } from './words';

/**
 * The checks that never ran (the reader they need is off). Quiet and closed by default: they are not
 * the AI's mistakes, and they are printed NOT CHECKED on the cloud PDF. One click clears them all.
 * A check whose run also lacked a setting links to that field on Company settings (#1373), only
 * when the API names it: the screen never works out which setting a check needs.
 */
export function NotCheckedGroup({
  items,
  note,
  busy,
  error,
  onClearAll,
  open: controlledOpen,
  onOpenChange,
}: {
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

/** The settings the check waits for, each a link to its field. One reads "Set it in Company settings". */
function SettingLinks({ settings }: { settings: readonly NotCheckedSetting[] }) {
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
