import { typedBy } from '@/lib/saved-values';

/**
 * One grey line (the wizard's Hint style) under a Measurements box about the value the server
 * already holds (#1183), when the box alone does not say it:
 * - the box was changed, so Save values replaces the saved value;
 * - the box was emptied, and a blank box keeps the saved value (the server keeps what is not sent).
 * Nothing when the box shows the saved value (a value saved for only some of its checks is said by
 * the field's review reason, "Saved for some of the checks it feeds").
 */
export function SavedValueNote({
  texts,
  setBy,
  setAt,
  showsSaved,
  blank,
}: {
  texts: readonly string[];
  setBy: string;
  setAt: string;
  showsSaved: boolean;
  blank: boolean;
}) {
  // A value saved for only some of its checks is said by the field's review reason instead.
  if (showsSaved) return null;
  const value = <span className="num text-foreground">{texts.join(', ')}</span>;
  return (
    <p data-part="saved-replaced" className="text-xs text-muted-foreground">
      Saved: {value}, {typedBy(setBy, setAt)}. {blank ? 'A blank box keeps it.' : 'Save values replaces it.'}
    </p>
  );
}
