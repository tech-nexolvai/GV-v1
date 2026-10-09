import type { ReactNode } from 'react';

import { uploadLabel, type SettingPointer } from './settingPointers.js';
import { Button } from '@/components/ui/button';
import { Hint, INPUT_CLASS } from './wizard-ui.js';

/**
 * "Found in the architect's drawing": type the value you see (#866, step 3.3 of #798).
 *
 * The app found the passage that states this setting, and shows where it is: the page, and a crop
 * of it. **It does not show the number it read there.** The reviewer reads the drawing and types
 * what it says, and the server saves the value only if the two match; a mismatch stops the save and
 * the server's sentence says so. A number shown here would turn that check into a copy of the app's
 * own reading, signed off by a person who never looked.
 *
 * **It cannot show the number, because it is never given one.** The pointer carries no value
 * (`SettingPointerOut`), and this reads only its page, its upload and whether a crop exists. The box
 * starts empty and holds only what the reviewer types. `tests/setting-citation.test.tsx` renders it
 * with a pointer that smuggles a number in and asserts the number never reaches the page.
 *
 * `crop` is the picture, loaded by the caller; this component makes no request.
 */
export function SettingCitation({
  name,
  pointer,
  value,
  crop,
  onChange,
  onDecline,
}: {
  name: string;
  pointer: SettingPointer;
  value: string;
  crop: ReactNode;
  onChange: (value: string) => void;
  onDecline: () => void;
}) {
  const page = pointer.page_index + 1;
  return (
    <div data-slot="setting-citation" className="flex flex-col gap-2">
      <p className="text-sm font-medium">
        Found in the architect&apos;s drawing, page {page}: type the value you see
      </p>
      {pointer.has_crop ? (
        crop
      ) : (
        <Hint>
          No picture of this passage was cut. Open page {page} of the {uploadLabel(pointer.document_kind)}{' '}
          and read it there.
        </Hint>
      )}
      <input
        className={`${INPUT_CLASS} num`}
        id={`p-${name}`}
        aria-label={`${name}, as the architect's drawing writes it on page ${page}`}
        placeholder="Type it as written, with its inch mark"
        autoComplete="off"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <Hint>
        Page {page} of the {uploadLabel(pointer.document_kind)}. The number the app read there is not
        shown: it is saved only if what you type matches the drawing.
      </Hint>
      <Button type="button" size="sm" variant="ghost" className="self-start" onClick={onDecline}>
        Enter it another way
      </Button>
    </div>
  );
}
