import { useState } from 'react';

import type { Finding } from '@/data/types';
import type { DecisionSaveResult, SimpleReviewAction } from '@/components/chat/decisionSave';
import { actionNeedsNote } from '@/components/output/reviewerResults';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';

type Choice = 'confirm' | 'problem' | 'dismiss';
type Problem = 'correct' | 'except';

export interface DecideHandlers {
  onAction: (id: string, action: SimpleReviewAction, note?: string) => Promise<DecisionSaveResult>;
  onCorrect: (id: string, value: string) => Promise<DecisionSaveResult>;
  onExcept: (id: string, reason: string, expiresAt: string) => Promise<DecisionSaveResult>;
}

/**
 * Record a reviewer decision on one finding (#1039): the three choices of the finding card, with
 * the same calls and the same rules — the note is required exactly where `actionNeedsNote` says,
 * a correction needs its value with a unit, an exception needs a reason and an end date. Nothing
 * here changes what a decision does; it only gathers it in one place.
 */
export function DecideDialog({
  finding,
  title,
  open,
  onOpenChange,
  handlers,
}: {
  finding: Pick<Finding, 'id' | 'outcome'> | null;
  title: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  handlers: DecideHandlers;
}) {
  const [choice, setChoice] = useState<Choice | ''>('');
  const [problem, setProblem] = useState<Problem | ''>('');
  const [note, setNote] = useState('');
  const [value, setValue] = useState('');
  const [expires, setExpires] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setChoice('');
    setProblem('');
    setNote('');
    setValue('');
    setExpires('');
    setError(null);
  }

  if (!finding) return null;
  const abstention = finding.outcome === 'REVIEW_REQUIRED' || finding.outcome === 'NOT_FOUND';
  const okLabel = abstention ? 'Checked: OK' : 'Confirm finding';
  const noLabel = abstention ? 'Not checkable' : 'Dismiss';
  const simple: SimpleReviewAction | null = choice === 'confirm' || choice === 'dismiss' ? choice : null;
  const noteRequired = simple !== null && actionNeedsNote(finding.outcome, simple);
  const today = new Date().toISOString().slice(0, 10);

  const ready =
    (simple !== null && (!noteRequired || note.trim().length > 0)) ||
    (choice === 'problem' && problem === 'correct' && value.trim().length > 0) ||
    (choice === 'problem' && problem === 'except' && note.trim().length > 0 && expires > today);

  async function save() {
    if (!finding || !ready || saving) return;
    setSaving(true);
    setError(null);
    try {
      let result: DecisionSaveResult;
      if (simple !== null) {
        result = await handlers.onAction(finding.id, simple, note.trim() || undefined);
      } else if (problem === 'correct') {
        result = await handlers.onCorrect(finding.id, value.trim());
      } else {
        // Midday rather than midnight, as on the finding card: a date input carries no time.
        result = await handlers.onExcept(finding.id, note.trim(), new Date(`${expires}T12:00:00Z`).toISOString());
      }
      if (result.saved) {
        reset();
        onOpenChange(false);
      } else {
        setError(result.error);
      }
    } catch (caught) {
      setError(`Save was not confirmed — ${caught instanceof Error ? caught.message : String(caught)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Decide: {title}</DialogTitle>
          <DialogDescription className="flex items-center gap-2">
            Recorded result <OutcomeBadge outcome={finding.outcome} />
          </DialogDescription>
        </DialogHeader>

        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <ToggleGroup
            type="single"
            variant="outline"
            value={choice}
            onValueChange={(next) => {
              setChoice(next as Choice | '');
              setError(null);
            }}
            aria-label="Decision"
            className="w-full"
          >
            <ToggleGroupItem value="confirm" className="flex-1">{okLabel}</ToggleGroupItem>
            <ToggleGroupItem value="problem" className="flex-1">Problem</ToggleGroupItem>
            <ToggleGroupItem value="dismiss" className="flex-1">{noLabel}</ToggleGroupItem>
          </ToggleGroup>

          {choice === 'problem' && (
            <ToggleGroup type="single" size="sm" variant="outline" value={problem} onValueChange={(next) => setProblem(next as Problem | '')} aria-label="Kind of problem">
              <ToggleGroupItem value="correct">Correct a value</ToggleGroupItem>
              <ToggleGroupItem value="except">Exception</ToggleGroupItem>
            </ToggleGroup>
          )}

          {simple !== null && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="decide-note">
                {simple === 'confirm' ? 'What did you check?' : finding.outcome === 'FAIL' ? 'Why are you dismissing this failure?' : 'Why is this not checkable?'}
                <span className="text-muted-foreground">{noteRequired ? ' (required)' : ' (optional)'}</span>
              </Label>
              <Textarea id="decide-note" value={note} required={noteRequired} onChange={(e) => setNote(e.target.value)} rows={3} />
            </div>
          )}

          {choice === 'problem' && problem === 'correct' && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="decide-value">Correct value, with its unit <span className="text-muted-foreground">(required)</span></Label>
              <Input id="decide-value" className="num" value={value} placeholder={'e.g. 25 1/2"'} onChange={(e) => setValue(e.target.value)} />
            </div>
          )}

          {choice === 'problem' && problem === 'except' && (
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="decide-reason">Why it is acceptable <span className="text-muted-foreground">(required)</span></Label>
                <Textarea id="decide-reason" value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="decide-expires">Until</Label>
                <Input id="decide-expires" type="date" min={today} value={expires} onChange={(e) => setExpires(e.target.value)} />
              </div>
            </div>
          )}

          {error && <p role="alert" className="text-sm text-outcome-fail-fg">{error}</p>}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!ready || saving}>{saving ? 'Saving…' : 'Record decision'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
