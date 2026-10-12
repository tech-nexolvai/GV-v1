/**
 * "New revision" (#1180): the vendor resubmitted. The same upload as starting a review, pre-filled
 * with the review's vendor and product (they do not change between revisions), with only the shop
 * drawing required. Leaving the architect's drawings empty reuses the previous revision's file.
 *
 * The upload path is `uploadRevision`: hash in the browser, `POST …/revisions` for the tickets (which
 * opens the revision and makes the previous one read-only), PUT, confirm, then queue the reading.
 */

import { useId, useState } from 'react';
import { FileText } from 'lucide-react';
import { uploadRevision, type UploadProgress } from '@/api/upload';
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
import { UploadProgressPanel } from '@/components/upload/upload-progress';
import { applyProgress, initialUploadView, type UploadView } from '@/lib/upload-progress';

function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
}

export interface NewRevisionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  packageId: string;
  vendor: string;
  product: string;
  /** The newest revision the screen shows. */
  currentRevision: number;
  /** Set when the newest revision was started but its drawing never uploaded: finish that one. */
  unfinished?: boolean;
  /** Called with the new revision's number once its drawings are in and reading is queued. */
  onStarted: (revisionNumber: number) => void;
  /** The upload, injectable for tests. */
  upload?: typeof uploadRevision;
}

export function NewRevisionDialog({
  open,
  onOpenChange,
  projectId,
  packageId,
  vendor,
  product,
  currentRevision,
  unfinished = false,
  onStarted,
  upload = uploadRevision,
}: NewRevisionDialogProps) {
  const [shop, setShop] = useState<File | null>(null);
  const [architectural, setArchitectural] = useState<File | null>(null);
  const [running, setRunning] = useState(false);
  const [view, setView] = useState<UploadView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const shopId = useId();
  const archId = useId();

  function choose(file: File | undefined, set: (file: File | null) => void) {
    if (!file) return;
    if (!isPdf(file)) {
      setError('Choose a PDF file.');
      return;
    }
    setError(null);
    set(file);
  }

  async function start() {
    if (!shop) return;
    setRunning(true);
    setError(null);
    setView(initialUploadView({ shop: shop.size, architectural: architectural?.size ?? 0 }));
    try {
      const number = await upload(projectId, packageId, { expectedRevision: currentRevision, shop, architectural }, (progress: UploadProgress) =>
        setView((current) => (current ? applyProgress(current, progress) : current)),
      );
      onStarted(number);
      onOpenChange(false);
      setShop(null);
      setArchitectural(null);
      setView(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setRunning(false);
    }
  }

  // Finishing an unfinished start uploads into that revision; otherwise the next one is opened.
  const next = unfinished ? currentRevision : currentRevision + 1;
  const previous = next - 1;
  return (
    <Dialog open={open} onOpenChange={(value) => { if (!running) onOpenChange(value); }}>
      <DialogContent className="font-sans sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {unfinished ? 'Finish revision ' : 'New revision '}<span className="num">{next}</span>
          </DialogTitle>
          <DialogDescription>
            {unfinished
              ? <>Revision <span className="num">{next}</span> was started but its drawing was never uploaded. Upload the vendor's re-issued shop drawing to finish it.</>
              : <>Upload the vendor's re-issued shop drawing. Revision <span className="num">{previous}</span> stays visible, read-only, exactly as it is.</>}{' '}
            A change to only the architect's drawings is not a new revision yet.
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Vendor</dt>
          <dd>{vendor}</dd>
          <dt className="text-muted-foreground">Product</dt>
          <dd>{product}</dd>
        </dl>

        {view ? (
          <UploadProgressPanel
            view={view}
            files={[
              { slot: 'shop' as const, title: 'Shop drawings', name: shop?.name ?? '', size: shop?.size ?? 0 },
              ...(architectural
                ? [{ slot: 'architectural' as const, title: "Architect's drawings", name: architectural.name, size: architectural.size }]
                : []),
            ]}
          />
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={shopId}>Shop drawings (required)</Label>
              <Input id={shopId} type="file" accept="application/pdf,.pdf" onChange={(event) => choose(event.target.files?.[0], setShop)} />
              {shop && (
                <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
                  <FileText className="size-4" aria-hidden="true" /> {shop.name}
                </span>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={archId}>Architect's drawings (optional)</Label>
              <Input id={archId} type="file" accept="application/pdf,.pdf" onChange={(event) => choose(event.target.files?.[0], setArchitectural)} />
              <span className="text-sm text-muted-foreground">
                {architectural
                  ? architectural.name
                  : `Leave empty to use the architect's drawings of revision ${previous} again.`}
              </span>
            </div>
          </div>
        )}

        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={running}>
            Cancel
          </Button>
          <Button onClick={() => void start()} disabled={!shop || running}>
            {running ? 'Uploading…' : unfinished ? `Finish revision ${next}` : `Start revision ${next}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
