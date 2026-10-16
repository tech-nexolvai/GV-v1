import { useId, useState, type ReactNode } from 'react';
import { ChevronRight, Equal, FileSearch, Ruler } from 'lucide-react';

import type { ArchitectCompareCounts, ArchitectCompareEntry, ArchitectCompareResult } from '@/api/review-items';
import { compareCounts, compareSummary, compareWhat, GROUP_OF, GROUP_TITLE, type CompareGroup } from './architect-compare-lib';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { NumText, PageTag } from './words';

/** A rule's recorded outcome, in the reviewer's words; anything else is shown as sent. */
const RULE_OUTCOME_WORDS: Record<string, string> = {
  PASS: 'looks right',
  FAIL: 'wrong',
  REVIEW_REQUIRED: 'needs you',
  NOT_FOUND: 'not checked',
  NO_APPLICABLE_RULE: 'no rule applies',
};

/** The glyph and word for one result: never colour alone. */
function ResultWord({ result }: { result: ArchitectCompareResult }) {
  if (result === 'different') {
    return (
      <span data-slot="compare-word" className="inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-fail-fg">
        <OutcomeIcon outcome="FAIL" size={14} />
        Different
      </span>
    );
  }
  if (result === 'unpaired' || result === 'unread') {
    return (
      <span data-slot="compare-word" className="inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-review-fg">
        <OutcomeIcon outcome="REVIEW_REQUIRED" size={14} />
        {result === 'unread' ? 'Not read' : 'No match'}
      </span>
    );
  }
  if (result === 'rule') {
    return (
      <span data-slot="compare-word" className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
        <Ruler aria-hidden="true" className="size-3.5" />
        By a rule
      </span>
    );
  }
  return (
    <span data-slot="compare-word" className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
      <Equal aria-hidden="true" className="size-3.5" />
      Same
    </span>
  );
}

/** The numbers side by side, in the number face. */
function Numbers({ entry }: { entry: ArchitectCompareEntry }) {
  return (
    <span className="block min-w-0 break-words text-[15px] text-foreground">
      {entry.vendor_text !== null && (
        <>
          Vendor <span className="num">{entry.vendor_text}</span> vs{' '}
        </>
      )}
      architect <span className="num">{entry.architect_text}</span>
      {entry.view && <span className="ml-2 text-[13px] text-muted-foreground">{entry.view}</span>}
      <PageTag page={entry.page} className="ml-2" />
    </span>
  );
}

function Row({ entry, onLook }: { entry: ArchitectCompareEntry; onLook: (entry: ArchitectCompareEntry) => void }) {
  const what = compareWhat(entry);
  const outcome = entry.rule_outcome ? (RULE_OUTCOME_WORDS[entry.rule_outcome] ?? entry.rule_outcome) : null;
  return (
    <li data-slot="compare-row" data-entry-key={entry.key} data-result={entry.result} className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 sm:px-6">
      <span className="shrink-0 basis-full sm:w-28 sm:basis-auto">
        <ResultWord result={entry.result} />
      </span>
      <span className="min-w-0 flex-1">
        <Numbers entry={entry} />
        {entry.result === 'rule' ? (
          <span data-slot="compare-rule" className="block text-[13px] leading-5 text-muted-foreground">
            <span className="num">{entry.rule_id ?? 'A rule'}</span>
            {outcome ? `: ${outcome}` : ': no outcome recorded yet'}
          </span>
        ) : (
          entry.result !== 'same' && <NumText text={entry.why} className="block text-[13px] leading-5 text-muted-foreground" />
        )}
      </span>
      <Button size="sm" variant="ghost" className="shrink-0" onClick={() => onLook(entry)} aria-label={`Look at ${what}, page ${entry.page}`}>
        <FileSearch aria-hidden="true" />
        Look
      </Button>
    </li>
  );
}

function Group({
  group,
  entries,
  defaultOpen,
  foldWords,
  onLook,
}: {
  group: CompareGroup;
  entries: readonly ArchitectCompareEntry[];
  defaultOpen: boolean;
  /** The fold button's words when closed, e.g. "Show all 80". */
  foldWords: (n: number) => string;
  onLook: (entry: ArchitectCompareEntry) => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const listId = useId();
  if (entries.length === 0) return null;
  const title = GROUP_TITLE[group];
  return (
    <div data-slot="compare-group" data-group={group} className="border-t border-border first:border-t-0">
      <h3>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((now) => !now)}
          className="flex min-h-11 w-full items-center gap-2 px-4 text-left text-[15px] font-medium outline-none focus-visible:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring sm:px-6"
        >
          <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none', open && 'rotate-90')} />
          {title}
          <span className="num text-[13px] font-normal text-muted-foreground">{entries.length}</span>
          {!open && <span className="ml-auto text-[13px] font-normal text-muted-foreground">{foldWords(entries.length)}</span>}
        </button>
      </h3>
      {open && (
        <ul id={listId} aria-label={title} className="divide-y divide-border border-t border-border">
          {entries.map((entry) => (
            <Row key={entry.key} entry={entry} onLook={onLook} />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * "Compared with the architect": every number the architect wrote, set beside the vendor's. Different
 * ones are open, the ones a person must settle (no match, not read) next, then the ones a rule checks,
 * and the same ones folded. Look opens both drawings with both numbers ringed. Hidden when the list
 * is empty (an older package). Only shows what the server compared; it works nothing out itself.
 */
export function ArchitectCompare({
  entries,
  counts,
  onLook,
}: {
  entries: readonly ArchitectCompareEntry[];
  counts: ArchitectCompareCounts | null;
  onLook: (entry: ArchitectCompareEntry) => void;
}) {
  if (entries.length === 0) return null;
  const shown = compareCounts(entries, counts);
  const by = (group: CompareGroup) => entries.filter((entry) => GROUP_OF[entry.result] === group);
  const groups: { group: CompareGroup; open: boolean; fold: (n: number) => string }[] = [
    { group: 'different', open: true, fold: () => 'Show' },
    { group: 'need_you', open: true, fold: () => 'Show' },
    { group: 'rule', open: false, fold: (n) => `Show ${n}` },
    { group: 'same', open: false, fold: (n) => `Show all ${n}` },
  ];
  const body: ReactNode[] = groups.map(({ group, open, fold }) => (
    <Group key={group} group={group} entries={by(group)} defaultOpen={open} foldWords={fold} onLook={onLook} />
  ));
  return (
    <section aria-labelledby="architect-compare-title" data-slot="architect-compare" className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h2 id="architect-compare-title" className="text-[20px] leading-7 font-semibold tracking-[-0.01em]">
          Compared with the architect
        </h2>
        <p data-testid="compare-summary" className="text-[13px] text-muted-foreground">
          <NumText text={compareSummary(shown)} />
        </p>
      </div>
      <div className="overflow-hidden rounded-xl border border-border">{body}</div>
    </section>
  );
}
