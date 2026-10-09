import { cn } from '@/lib/utils';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import type { Choice, DecisionDraft, Problem } from './use-decision-draft';

/** The fields of a decision. `big` is the queue's look: three large buttons in a row. */
export function DecisionFields({
  draft,
  idPrefix,
  big = false,
  noteRef,
  allowProblem = true,
}: {
  draft: DecisionDraft;
  idPrefix: string;
  big?: boolean;
  noteRef?: React.Ref<HTMLTextAreaElement>;
  /** False where nothing was read, so there is no value to correct (a split page, #1093). */
  allowProblem?: boolean;
}) {
  const { outcome, choice, problem, simple, noteRequired, labels, today } = draft;
  const choiceClass = big ? 'h-12 flex-1 text-sm font-medium' : 'flex-1';
  return (
    <>
      <ToggleGroup
        type="single"
        variant="outline"
        value={choice}
        onValueChange={(next) => draft.choose(next as Choice | '')}
        aria-label="Decision"
        className="w-full"
      >
        <ToggleGroupItem value="confirm" className={choiceClass}>
          {big && <kbd className="num mr-1.5 hidden rounded border px-1 text-[10px] text-muted-foreground sm:inline">1</kbd>}
          {labels.confirm}
        </ToggleGroupItem>
        {allowProblem && (
          <ToggleGroupItem value="problem" className={choiceClass}>
            {big && <kbd className="num mr-1.5 hidden rounded border px-1 text-[10px] text-muted-foreground sm:inline">2</kbd>}
            {labels.problem}
          </ToggleGroupItem>
        )}
        <ToggleGroupItem value="dismiss" className={choiceClass}>
          {big && <kbd className="num mr-1.5 hidden rounded border px-1 text-[10px] text-muted-foreground sm:inline">3</kbd>}
          {labels.dismiss}
        </ToggleGroupItem>
      </ToggleGroup>

      {allowProblem && choice === 'problem' && (
        <ToggleGroup type="single" size="sm" variant="outline" value={problem} onValueChange={(next) => draft.setProblem(next as Problem | '')} aria-label="Kind of problem">
          <ToggleGroupItem value="correct">Correct a value</ToggleGroupItem>
          <ToggleGroupItem value="except">Exception</ToggleGroupItem>
        </ToggleGroup>
      )}

      {simple !== null && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${idPrefix}-note`}>
            {simple === 'confirm' ? 'What did you check?' : outcome === 'FAIL' ? 'Why are you dismissing this failure?' : 'Why is this not checkable?'}
            <span className="text-muted-foreground">{noteRequired ? ' (required)' : ' (optional)'}</span>
          </Label>
          <Textarea ref={noteRef} id={`${idPrefix}-note`} value={draft.note} required={noteRequired} onChange={(e) => draft.setNote(e.target.value)} rows={big ? 2 : 3} />
        </div>
      )}

      {choice === 'problem' && problem === 'correct' && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${idPrefix}-value`}>
            Correct value, with its unit <span className="text-muted-foreground">(required)</span>
          </Label>
          <Input id={`${idPrefix}-value`} className="num" value={draft.value} placeholder={'e.g. 25 1/2"'} onChange={(e) => draft.setValue(e.target.value)} />
        </div>
      )}

      {choice === 'problem' && problem === 'except' && (
        <div className={cn('grid gap-3', 'sm:grid-cols-[minmax(0,1fr)_auto]')}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${idPrefix}-reason`}>
              Why it is acceptable <span className="text-muted-foreground">(required)</span>
            </Label>
            <Textarea ref={noteRef} id={`${idPrefix}-reason`} value={draft.note} onChange={(e) => draft.setNote(e.target.value)} rows={2} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${idPrefix}-expires`}>Until</Label>
            <Input id={`${idPrefix}-expires`} type="date" min={today} value={draft.expires} onChange={(e) => draft.setExpires(e.target.value)} />
          </div>
        </div>
      )}

      {draft.error && (
        <p role="alert" className="text-sm text-outcome-fail-fg">
          {draft.error}
        </p>
      )}
    </>
  );
}
