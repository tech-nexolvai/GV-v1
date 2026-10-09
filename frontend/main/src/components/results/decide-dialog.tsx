import type { Finding } from '@/data/types';
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
import { DecisionFields } from './decision-form';
import { useDecisionDraft, type DecideHandlers } from './use-decision-draft';

export type { DecideHandlers } from './use-decision-draft';

/**
 * Record a reviewer decision on one finding (#1039): the three choices of the finding card, with
 * the same calls and the same rules. The form itself is shared with the "Needs you" queue (#1050),
 * so the two cannot drift; nothing here changes what a decision does.
 */
export function DecideDialog({
  finding,
  title,
  open,
  onOpenChange,
  handlers,
  allowProblem = true,
}: {
  finding: Pick<Finding, 'id' | 'outcome'> | null;
  title: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  handlers: DecideHandlers;
  /** False for a split page (#1093): checked, or not checkable; nothing was read to correct. */
  allowProblem?: boolean;
}) {
  const draft = useDecisionDraft(finding);
  if (!finding) return null;

  async function save() {
    if (await draft.save(handlers)) onOpenChange(false);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) draft.reset();
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
          <DecisionFields draft={draft} idPrefix="decide" allowProblem={allowProblem} />
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                // Cancel forgets the draft, so the next finding never opens with this one's choice and note.
                draft.reset();
                onOpenChange(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!draft.ready || draft.saving}>{draft.saving ? 'Saving…' : 'Record decision'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
