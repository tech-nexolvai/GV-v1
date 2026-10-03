import { RefreshCw, ScanLine } from 'lucide-react';
import { uploadLabel, type SettingPointer } from './settingPointers.js';

export type PassageImage = { url: string } | { error: true } | null;

/** Pixels and location only; never presents a parsed answer or changes the entered value. */
export function SettingPassage({ pointer, name, image, onRetry, onImageError }: {
  pointer: SettingPointer;
  name: string;
  image: PassageImage;
  onRetry: () => void;
  onImageError: () => void;
}) {
  const page = pointer.page_index + 1;
  if (image && 'error' in image) return (
    <div className="setting-citation__recovery">
      <p className="setting-citation__note" role="alert">
        The picture of this passage could not be loaded. Retry, or open page {page} of the{' '}
        {uploadLabel(pointer.document_kind)} and read it there.
      </p>
      <button type="button" className="value-secondary" onClick={onRetry}>
        <RefreshCw size={14} aria-hidden="true" /> Retry passage image
      </button>
    </div>
  );
  if (!image) return (
    <span className="ai-proposal__crop-loading" role="status">
      <ScanLine size={14} aria-hidden="true" /> Loading the passage…
    </span>
  );
  return (
    <figure className="layout-crop">
      <img className="setting-citation__crop" src={image.url} onError={onImageError}
        alt={`The passage on page ${page} where the architect's drawing states ${name}`} />
      <figcaption>Page {page} of the {uploadLabel(pointer.document_kind)}</figcaption>
    </figure>
  );
}
