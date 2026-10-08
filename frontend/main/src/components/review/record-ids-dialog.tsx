import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

/**
 * The record identifiers a reviewer may need to quote (support, the signed report), out of the way
 * in a dialog opened from the review's "More actions" menu. Replaces "Details & steps" (#1034).
 */
export function RecordIdsDialog({
  open,
  onOpenChange,
  packageId,
  projectId,
  revisionId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  packageId: string;
  projectId: string;
  revisionId: string | null;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Record IDs</DialogTitle>
          <DialogDescription>Quote these when asking about this review.</DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">Package</dt>
          <dd className="num break-all select-all">{packageId}</dd>
          <dt className="text-muted-foreground">Project</dt>
          <dd className="num break-all select-all">{projectId}</dd>
          {revisionId && (
            <>
              <dt className="text-muted-foreground">Revision</dt>
              <dd className="num break-all select-all">{revisionId}</dd>
            </>
          )}
        </dl>
      </DialogContent>
    </Dialog>
  );
}
