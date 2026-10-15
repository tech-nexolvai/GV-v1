import { useState } from 'react';
import { Info } from 'lucide-react';

import type { Finding } from '@/data/types';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { bulkEligible, decisionWords } from '@/lib/countertop-results';
import { CarriedOver } from './carried-over';
import { NeedsValues } from './needs-values';

export interface BulkResult {
  saved: number;
  failed: { id: string; error: string }[];
}

/**
 * The checks that are not countertop rows (#1039) — typically package-level checks waiting on data
 * the drawings do not carry. Collapsed by default, except where they are the whole review (a cabinet
 * package, #1205). One "Mark not checkable…" asks for a single note,
 * shows exactly which checks it will touch, and records the same dismissal on each (one call per
 * finding, the usual rules); a failure is never swept up in it.
 */
export function OtherChecks({
  findings,
  ruleNames,
  blocking,
  onDecide,
  onBulkDismiss,
  packageId,
  onValuesSaved,
  onRunChecks,
  defaultOpen = false,
  title = 'Other checks',
}: {
  findings: Finding[];
  /** Rule id → its human name from the rulebook; the id is shown when a name is not known. */
  ruleNames: ReadonlyMap<string, string>;
  blocking: ReadonlySet<string>;
  onDecide: (finding: Finding) => void;
  onBulkDismiss: (ids: string[], note: string) => Promise<BulkResult>;
  /** With these, a check waiting on a value asks for it in place (#1181). */
  packageId?: string;
  onValuesSaved?: () => void;
  onRunChecks?: () => void;
  /** Opened at first: on a cabinet package these are the review's checks (#1205). */
  defaultOpen?: boolean;
  /** "Whole package" when the results are grouped per confirmed countertop (#1245). */
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<BulkResult | null>(null);
  if (findings.length === 0) return null;

  const eligible = bulkEligible(findings, blocking);
  const nameOf = (f: Finding) => ruleNames.get(f.check_id) ?? (f.scope_label && f.scope_label !== 'Package revision' ? f.scope_label : f.name);
  const waiting = findings.filter((f) => blocking.has(f.id)).length;

  async function confirm() {
    if (!note.trim() || saving) return;
    setSaving(true);
    try {
      const outcome = await onBulkDismiss(eligible.map((f) => f.id), note.trim());
      setResult(outcome);
      if (outcome.failed.length === 0) {
        setOpen(false);
        setNote('');
        setResult(null);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <TooltipProvider delayDuration={250}>
    <section data-slot="other-checks" aria-label={title}>
      <Accordion type="single" collapsible defaultValue={defaultOpen ? 'other' : undefined} className="rounded-xl border bg-card px-4">
        <AccordionItem value="other" className="border-b-0">
          <AccordionTrigger className="py-3">
            <span className="flex items-center gap-2">
              {title}
              <span className="num rounded-full bg-muted px-1.5 text-xs">{findings.length}</span>
              {/* Its noun with it (#1179): these are checks, apart from the countertops and the review's items. */}
              {waiting > 0 && <span className="text-xs text-outcome-review-fg"><span className="num">{waiting}</span> {waiting === 1 ? 'check needs' : 'checks need'} you</span>}
            </span>
          </AccordionTrigger>
          <AccordionContent className="flex flex-col gap-3">
            {eligible.length > 0 && (
              <div>
                <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
                  Mark not checkable… <span className="num">({eligible.length})</span>
                </Button>
              </div>
            )}
            <ul className="divide-y rounded-lg border">
              {findings.map((finding) => (
                <li key={finding.id} className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2 text-sm">
                  <OutcomeBadge outcome={finding.outcome} />
                  <span className="min-w-0 flex-1 truncate">{nameOf(finding)}</span>
                  {finding.reason && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button type="button" aria-label={`Why: ${finding.reason}`} className="rounded text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                          <Info className="size-3.5" aria-hidden="true" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-80">{finding.reason}</TooltipContent>
                    </Tooltip>
                  )}
                  <span className="num hidden text-xs text-muted-foreground sm:inline">{finding.check_id}</span>
                  {finding.reviewer_action && !blocking.has(finding.id) ? (
                    <span className="text-xs text-muted-foreground">
                      {decisionWords(finding.reviewer_action, finding.outcome)}
                      {finding.reviewer_carried_over && <> · <CarriedOver /></>}
                    </span>
                  ) : blocking.has(finding.id) ? (
                    <Button size="sm" variant="ghost" onClick={() => onDecide(finding)}>Decide</Button>
                  ) : null}
                  {packageId && blocking.has(finding.id) && ((finding.missing_inputs?.length ?? 0) > 0 || (finding.depth_readings?.length ?? 0) > 0) && (
                    <div className="basis-full">
                      <NeedsValues finding={finding} packageId={packageId} onSaved={() => onValuesSaved?.()} onRunChecks={onRunChecks} />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </AccordionContent>
        </AccordionItem>
      </Accordion>

      <Dialog
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setResult(null);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Mark {eligible.length} {eligible.length === 1 ? 'check' : 'checks'} not checkable</DialogTitle>
            <DialogDescription>The same note is recorded on each one, as your decision.</DialogDescription>
          </DialogHeader>
          <ul className="max-h-40 overflow-y-auto rounded-md border text-sm" aria-label="Checks to mark">
            {eligible.map((f) => (
              <li key={f.id} className="flex items-center gap-2 border-b px-3 py-1.5 last:border-b-0">
                <span className="min-w-0 flex-1 truncate">{nameOf(f)}</span>
                <span className="num text-xs text-muted-foreground">{f.check_id}</span>
              </li>
            ))}
          </ul>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="bulk-note">Why are these not checkable? <span className="text-muted-foreground">(required)</span></Label>
            <Textarea id="bulk-note" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
          {result && result.failed.length > 0 && (
            <p role="alert" className="text-sm text-outcome-fail-fg">
              <span className="num">{result.saved}</span> recorded, <span className="num">{result.failed.length}</span> not: {result.failed[0].error}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button onClick={() => void confirm()} disabled={!note.trim() || saving}>
              {saving ? 'Recording…' : `Mark ${eligible.length} not checkable`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
    </TooltipProvider>
  );
}
