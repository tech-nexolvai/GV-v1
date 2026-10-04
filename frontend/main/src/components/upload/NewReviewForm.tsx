/**
 * Start a review: who sent the drawings, the architect's set, and the vendor's shop drawings.
 *
 * This is the first thing on the start screen, where a chat product puts its composer — starting a
 * review *is* the first message. It replaces a modal that opened with a made-up vendor already typed
 * in ("Apex Glass & Stone"), which is how a sample name ends up on a real package.
 *
 * The upload path is unchanged (`createPackage`): the browser hashes each file, the API hands back a
 * ticket, the bytes go straight to storage, and the API confirms them. The progress line shows the
 * step that is really running, never a staged imitation.
 */

import { useRef, useState } from 'react';
import { ArrowUp, FileText, Loader2, Plus, X } from 'lucide-react';
import { createPackage } from '../../api/upload';
import type { UploadProgress } from '../../api/upload';
import { describeUploadFailure } from '../../api/uploadState';
import { projectId } from '../../api/config';
import './NewReviewForm.css';

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
  const [files, setFiles] = useState<Record<Slot, File | null>>({ architectural: null, shop: null });
  const [slotError, setSlotError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState<Slot | null>(null);
  const [running, setRunning] = useState(false);
  const [step, setStep] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [savedPackageId, setSavedPackageId] = useState<string | null>(null);
  // Hooks must be called directly: the React compiler can memoize an object literal and skip
  // hooks nested inside it on the next render.
  const architecturalInput = useRef<HTMLInputElement>(null);
  const shopInput = useRef<HTMLInputElement>(null);

  const ready = vendor.trim() !== '' && files.architectural !== null && files.shop !== null;
  const missing = [
    vendor.trim() === '' ? 'the vendor' : null,
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
    if (!ready || running) return;
    setRunning(true);
    setError(null);
    setSavedPackageId(null);
    setStep('');
    try {
      const { packageId } = await createPackage(
        projectId(),
        { vendor: vendor.trim(), architectural: files.architectural, shop: files.shop },
        (progress: UploadProgress) => {
          setStep(progress.file ? `${progress.step} ${progress.file}` : progress.step);
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

  if (running) {
    return (
      <div className="new-review new-review--running" role="status" aria-live="polite">
        <Loader2 size={20} className="new-review__spinner" aria-hidden="true" />
        <div>
          <p className="new-review__step">{step || 'Starting…'}</p>
          <p className="new-review__step-note">
            The review opens when both drawings are uploaded and confirmed.
          </p>
        </div>
      </div>
    );
  }

  return (
    <form
      className="new-review"
      onSubmit={(event) => {
        event.preventDefault();
        void start();
      }}
    >
      <label className="new-review__vendor">
        <span className="new-review__label">Vendor</span>
        <input
          className="new-review__vendor-input"
          value={vendor}
          onChange={(event) => setVendor(event.target.value)}
          placeholder="Who sent these drawings?"
          autoComplete="organization"
          required
        />
      </label>

      <div className="new-review__slots">
        {(Object.keys(SLOT_COPY) as Slot[]).map((slot) => {
          const file = files[slot];
          return (
            <div
              key={slot}
              className="new-review__slot"
              data-filled={file !== null}
              data-drag={dragOver === slot}
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
                  className="new-review__slot-pick"
                  onClick={() => (slot === 'architectural' ? architecturalInput : shopInput).current?.click()}
                >
                  <Plus size={16} aria-hidden="true" />
                  <span className="new-review__slot-text">
                    <span className="new-review__slot-title">{SLOT_COPY[slot].title}</span>
                    <span className="new-review__slot-hint">{SLOT_COPY[slot].hint} · PDF</span>
                  </span>
                </button>
              ) : (
                <div className="new-review__file">
                  <FileText size={16} aria-hidden="true" className="new-review__file-icon" />
                  <span className="new-review__slot-text">
                    <span className="new-review__slot-title">{SLOT_COPY[slot].title}</span>
                    <span className="new-review__file-name" title={file.name}>
                      {file.name} · {formatSize(file.size)}
                    </span>
                  </span>
                  <button
                    type="button"
                    className="new-review__file-remove"
                    onClick={() => setFiles((current) => ({ ...current, [slot]: null }))}
                    aria-label={`Remove ${SLOT_COPY[slot].title}`}
                  >
                    <X size={14} />
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="new-review__foot">
        <p className="new-review__need" aria-live="polite">
          {ready
            ? 'Ready to upload. You will review the readings and any values still needed.'
            : `Add ${missing.join(', ').replace(/, ([^,]*)$/, ' and $1')}.`}
        </p>
        <button
          type="submit"
          className="new-review__send"
          disabled={!ready}
          aria-label="Start review"
          title="Start review"
        >
          <ArrowUp size={18} />
        </button>
      </div>

      {slotError && (
        <p className="new-review__error" role="alert">{slotError}</p>
      )}
      {error && (
        <div className="new-review__error" role="alert">
          <strong>The submission did not finish.</strong>{' '}
          {savedPackageId
            ? 'A document set was created and may contain an uploaded drawing. Open that set to inspect it; starting again here creates another set.'
            : 'We could not confirm whether a document set was created. Check Documents before trying again.'}{' '}
          <span className="new-review__error-detail">{error}</span>
          {savedPackageId && (
            <button type="button" className="btn btn--ghost new-review__open-saved" onClick={() => onCreated(savedPackageId)}>
              Open saved document set
            </button>
          )}
        </div>
      )}
    </form>
  );
}
