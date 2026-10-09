import type { DocumentKind, UploadProgress } from '@/api/upload';

/**
 * What the start screen shows while a review is being created (#1064): one bar per drawing and a
 * three-step line (Upload → AI reading → Ready). Built only from the steps `createPackage` reports,
 * so every bar is the step that is really running, never a staged imitation.
 */
export type Slot = Extract<DocumentKind, 'architectural' | 'shop'>;

export type FilePhase = 'waiting' | 'preparing' | 'sending' | 'confirming' | 'done' | 'combined';

export interface FileProgress {
  phase: FilePhase;
  loaded: number;
  total: number;
}

export interface UploadView {
  files: Record<Slot, FileProgress>;
  /** `reading` once both drawings are confirmed and the AI reading has been asked for. */
  stage: 'upload' | 'reading';
  /** The current step in words, for the live region (it does not repeat every percentage). */
  step: string;
}

export function initialUploadView(sizes: Record<Slot, number>): UploadView {
  return {
    files: {
      architectural: { phase: 'waiting', loaded: 0, total: sizes.architectural },
      shop: { phase: 'waiting', loaded: 0, total: sizes.shop },
    },
    stage: 'upload',
    step: 'Starting…',
  };
}

const PHASE_OF_STEP: Record<string, FilePhase> = {
  Hashing: 'preparing',
  Registering: 'preparing',
  Uploading: 'sending',
  Confirming: 'confirming',
  Uploaded: 'done',
  'Same file as the shop drawings': 'combined',
};

function isSlot(kind: DocumentKind | undefined): kind is Slot {
  return kind === 'architectural' || kind === 'shop';
}

export function applyProgress(view: UploadView, progress: UploadProgress): UploadView {
  const step = progress.file ? `${progress.step} ${progress.file}` : progress.step;
  if (progress.step === 'Queuing AI reading' || progress.step === 'Opening review') {
    return { ...view, stage: 'reading', step };
  }
  const phase = PHASE_OF_STEP[progress.step];
  if (!phase || !isSlot(progress.kind)) return { ...view, step };
  const before = view.files[progress.kind];
  const total = progress.total ?? before.total;
  const loaded =
    phase === 'sending' ? (progress.loaded ?? 0) : phase === 'confirming' || phase === 'done' ? total : before.loaded;
  return {
    ...view,
    // A percentage update keeps the words as they were, so a screen reader is not read every tick.
    step: phase === 'sending' && before.phase === 'sending' ? view.step : step,
    files: { ...view.files, [progress.kind]: { phase, loaded, total } },
  };
}

/** 0–100. Sending is the real share of bytes sent; confirming and done are 100. */
export function percentOf(file: FileProgress): number {
  if (file.phase === 'confirming' || file.phase === 'done') return 100;
  if (file.phase !== 'sending' || file.total <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((file.loaded / file.total) * 100)));
}
