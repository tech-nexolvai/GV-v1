import { GV_MARKS_WARNING, showsGvMarks, type PictureGvMarks } from './drawingPartChoices.js';
import { TriangleAlert } from 'lucide-react';

/**
 * The plain warning under a part's picture that shows GV's own coloured marks (#921).
 *
 * GV's red and yellow markup is sometimes baked into the vendor's drawing itself, where the picture
 * cannot leave it out, so a person confirming a part could be looking at GV's correction rather than
 * the vendor's drawing. Shown only where the check found such marks; it changes nothing else.
 */
export function GvMarksWarning({ marks }: { marks: PictureGvMarks | null }) {
  if (!showsGvMarks(marks)) return null;
  return (
    <p className="drawing-parts__gv-marks" role="note">
      <TriangleAlert size={16} aria-hidden="true" />
      {GV_MARKS_WARNING}
    </p>
  );
}
