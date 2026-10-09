/**
 * Start a review: who sent the drawings, what product they are for, the architect's set, and the
 * vendor's shop drawings.
 *
 * The first thing on the start screen. It replaced a modal that opened with a made-up vendor already
 * typed in ("Apex Glass & Stone"), which is how a sample name ends up on a real package. Since #1125
 * it is built from the shadcn inputs, with two drop zones and a real "Start review" button that says
 * what is still missing while it is disabled.
 *
 * The upload path is unchanged (`createPackage`): the browser hashes each file, the API hands back a
 * ticket, the bytes go straight to storage, and the API confirms them. While it runs, the progress
 * panel shows each drawing's real share of bytes sent and the step that is really running (#1064).
 */

import { useId, useRef, useState } from 'react';
import { ArrowRight, FileText, Upload, X } from 'lucide-react';
import { listProductTypes } from '@/api/client';
import { createPackage } from '@/api/upload';
import type { UploadProgress } from '@/api/upload';
import { useAsync } from '@/api/useAsync';
import {
  defaultProductType,
  describeUploadFailure,
  looksLikeTheSameFile,
} from '@/api/uploadState';
import type { ProductType } from '@/api/uploadState';
import { projectId } from '@/api/config';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { cn } from '@/lib/utils';
import { applyProgress, initialUploadView, type UploadView } from '@/lib/upload-progress';
import { ProductTypeField } from './ProductTypeField';
import type { ProductChoicesState } from './ProductTypeField';
import { UploadProgressPanel } from './upload-progress';

interface NewReviewFormProps {
  onCreated: (packageId: string) => void;
}

type Slot = 'architectural' | 'shop';

const SLOT_COPY: Record<Slot, { title: string; hint: string }> = {
  architectural: { title: "Architect's drawings", hint: 'The design set the work must match' },
  shop: { title: 'Shop drawings', hint: "The vendor's drawings to check" },
};

function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
}

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function NewReviewForm({ onCreated }: NewReviewFormProps) {
  const [vendor, setVendor] = useState('');
  const products = useAsync(() => listProductTypes(), []);
  const [chosenProduct, setChosenProduct] = useState<ProductType | null>(null);
  const [files, setFiles] = useState<Record<Slot, File | null>>({ architectural: null, shop: null });
  const [slotError, setSlotError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState<Slot | null>(null);
  const [running, setRunning] = useState(false);
  // What the progress panel shows (#1064): one bar per drawing and the Upload → AI reading → Ready steps.
  const [view, setView] = useState<UploadView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedPackageId, setSavedPackageId] = useState<string | null>(null);
  // Hooks must be called directly: the React compiler can memoize an object literal and skip
  // hooks nested inside it on the next render.
  const architecturalInput = useRef<HTMLInputElement>(null);
  const shopInput = useRef<HTMLInputElement>(null);
  const vendorId = useId();
  const needId = useId();

  const productState: ProductChoicesState =
    products.status === 'ready'
      ? { status: 'ready', choices: products.data }
      : products.status === 'error'
      ? { status: 'error', message: products.error.message }
      : { status: 'loading' };
  // The reviewer's choice, or Countertop until they make one (#994). Only a product the API offered.
  const productType =
    products.status === 'ready'
      ? (products.data.some((choice) => choice.value === chosenProduct)
          ? chosenProduct
          : defaultProductType(products.data))
      : null;

  const ready =
    vendor.trim() !== '' &&
    productType !== null &&
    files.architectural !== null &&
    files.shop !== null;
  const sameFile = looksLikeTheSameFile(files.architectural, files.shop);
  const missing = [
    vendor.trim() === '' ? 'the vendor' : null,
    productType === null ? 'what the drawing set is for' : null,
    files.architectural === null ? "the architect's drawings" : null,
    files.shop === null ? 'the shop drawings' : null,
  ].filter(Boolean) as string[];

  function choose(slot: Slot, file: File | null | undefined) {
    if (!file) return;
    if (!isPdf(file)) {
      setSlotError(`${file.name} is not a PDF. Only PDF drawings can be reviewed.`);
      return;
    }
    setSlotError(null);
    setFiles((current) => ({ ...current, [slot]: file }));
  }

  async function start() {
    if (!ready || running || productType === null) return;
    setRunning(true);
    setError(null);
    setSavedPackageId(null);
    setView(initialUploadView({ architectural: files.architectural?.size ?? 0, shop: files.shop?.size ?? 0 }));
    try {
      const { packageId } = await createPackage(
        projectId(),
        {
          vendor: vendor.trim(),
          productType,
          architectural: files.architectural,
          shop: files.shop,
        },
        (progress: UploadProgress) => {
          setView((current) => (current ? applyProgress(current, progress) : current));
        },
      );
      onCreated(packageId);
    } catch (failure) {
      // A package may have been created before a later upload step failed. Keep its identity visible.
      const described = describeUploadFailure(failure);
      setRunning(false);
      setError(described.detail);
      setSavedPackageId(described.savedPackageId);
    }
  }

  if (running && view) {
    return (
      <div data-tw className="rounded-xl border bg-card p-4 font-sans text-card-foreground shadow-xs sm:p-6">
        <UploadProgressPanel
          view={view}
          files={(Object.keys(SLOT_COPY) as Slot[])
            .filter((slot) => files[slot] !== null)
            .map((slot) => ({ slot, title: SLOT_COPY[slot].title, name: files[slot]!.name, size: files[slot]!.size }))}
        />
      </div>
    );
  }

  return (
    <form
      data-tw
      data-slot="new-review-form"
      className="flex flex-col gap-5 rounded-xl border bg-card p-4 font-sans text-card-foreground shadow-xs sm:p-6"
      onSubmit={(event) => {
        event.preventDefault();
        void start();
      }}
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor={vendorId}>Vendor</Label>
        <Input
          id={vendorId}
          value={vendor}
          onChange={(event) => setVendor(event.target.value)}
          placeholder="The company that sent the drawings"
          autoComplete="organization"
          required
        />
      </div>

      <ProductTypeField state={productState} value={productType} onChange={setChosenProduct} />

      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="mb-2 text-sm font-medium">
          Drawings <span className="font-normal text-muted-foreground">· one PDF in each box</span>
        </legend>
        <div className="grid gap-3 sm:grid-cols-2">
          {(Object.keys(SLOT_COPY) as Slot[]).map((slot) => {
            const file = files[slot];
            return (
              <div
                key={slot}
                data-drop-zone={slot}
                data-filled={file !== null}
                data-drag={dragOver === slot}
                className={cn(
                  'flex min-h-24 min-w-0 rounded-lg border border-dashed border-input transition-colors',
                  file !== null && 'border-solid border-border bg-muted/50',
                  dragOver === slot && 'border-solid border-foreground bg-accent',
                )}
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragOver(slot);
                }}
                onDragLeave={() => setDragOver(null)}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragOver(null);
                  choose(slot, event.dataTransfer.files?.[0]);
                }}
              >
                <input
                  ref={slot === 'architectural' ? architecturalInput : shopInput}
                  type="file"
                  accept="application/pdf,.pdf"
                  hidden
                  onChange={(event) => {
                    choose(slot, event.target.files?.[0]);
                    event.target.value = '';
                  }}
                />
                {file === null ? (
                  <button
                    type="button"
                    className="flex w-full flex-col items-center justify-center gap-1 rounded-lg p-4 text-center outline-none hover:bg-accent/60 focus-visible:ring-[3px] focus-visible:ring-ring/50"
                    onClick={() => (slot === 'architectural' ? architecturalInput : shopInput).current?.click()}
                  >
                    <Upload className="mb-1 size-5 text-muted-foreground" aria-hidden="true" />
                    <span className="text-sm font-medium">{SLOT_COPY[slot].title}</span>
                    <span className="text-xs text-muted-foreground">{SLOT_COPY[slot].hint}</span>
                  </button>
                ) : (
                  <div className="flex w-full min-w-0 items-center gap-3 p-3">
                    <FileText className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="text-sm font-medium">{SLOT_COPY[slot].title}</span>
                      <span className="flex min-w-0 items-baseline gap-1 text-xs text-muted-foreground">
                        <span className="truncate" title={file.name}>{file.name}</span>
                        <span className="num shrink-0">· {formatSize(file.size)}</span>
                      </span>
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      className="pointer-coarse:size-11"
                      onClick={() => setFiles((current) => ({ ...current, [slot]: null }))}
                      aria-label={`Remove ${SLOT_COPY[slot].title}`}
                    >
                      <X aria-hidden="true" />
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </fieldset>

      {slotError && (
        <p className="text-sm text-destructive" role="alert">{slotError}</p>
      )}

      <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
        <p id={needId} className="text-sm text-muted-foreground" aria-live="polite">
          {ready && sameFile
            ? "You chose the same file twice. It will be read once, as one combined set, and you will confirm which drawings on it are the architect's and which are the vendor's."
            : ready
            ? 'Ready to upload. You will review the readings and any values still needed.'
            : `Add ${missing.join(', ').replace(/, ([^,]*)$/, ' and $1')}.`}
        </p>
        <Button type="submit" disabled={!ready} aria-describedby={needId} className="w-full shrink-0 sm:w-auto">
          Start review <ArrowRight aria-hidden="true" />
        </Button>
      </div>

      {error && (
        <div className="flex flex-col items-start gap-2 rounded-lg border border-destructive/50 p-3 text-sm" role="alert">
          <p>
            <strong className="font-semibold">The submission did not finish.</strong>{' '}
            {savedPackageId
              ? 'A document set was created and may contain an uploaded drawing. Open that set to inspect it; starting again here creates another set.'
              : 'We could not confirm whether a document set was created. Check Documents before trying again.'}
          </p>
          <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{error}</p>
          {savedPackageId && (
            <Button type="button" variant="outline" size="sm" onClick={() => onCreated(savedPackageId)}>
              Open saved document set
            </Button>
          )}
        </div>
      )}
    </form>
  );
}
