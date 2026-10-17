import { useId, useState, type FormEvent } from 'react';

import type { ChatTaskEdit, ChatUnderstood } from '@/lib/chat-api';
import { formOf, SHAPE_CHOICES, taskEditOf, understoodWords, type UnderstoodForm } from '@/lib/chat-understood';

/**
 * #1551: a quiet line under a typed question, "Understood as: Compare outlet heights (architect vs
 * vendor) · page 4 · short answer · Edit". Edit opens a small form (what to check, what to compare,
 * pages, answer length); Resend asks the same question again with the correction, which the server
 * uses as given. Read-only: nothing here changes the review.
 */
export function UnderstoodLine({
  understood,
  onResend,
  disabled = false,
}: {
  understood: ChatUnderstood;
  /** Absent: no Edit (e.g. while an answer is running). */
  onResend?: (task: ChatTaskEdit) => void;
  disabled?: boolean;
}) {
  const [form, setForm] = useState<UnderstoodForm | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const formId = useId();
  const canEdit = Boolean(onResend) && !disabled;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!form || !onResend) return;
    const edit = taskEditOf(understood, form);
    if (typeof edit === 'string') {
      setProblem(edit);
      return;
    }
    setForm(null);
    setProblem(null);
    onResend(edit);
  }

  if (form) {
    const field = 'h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50';
    return (
      <form
        data-slot="chat-understood-edit"
        aria-label="Correct how the question was understood"
        onSubmit={submit}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setForm(null);
            setProblem(null);
          }
        }}
        className="flex w-full max-w-[min(85%,36rem)] flex-col gap-2 self-end rounded-xl border bg-background p-3 text-sm"
      >
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">What to check</span>
          <input
            className={field}
            value={form.task}
            maxLength={200}
            autoFocus
            placeholder="e.g. Compare outlet heights with the architect"
            onChange={(event) => setForm({ ...form, task: event.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Compare (comma between items, optional)</span>
          <input className={field} value={form.compare} onChange={(event) => setForm({ ...form, compare: event.target.value })} />
        </label>
        <div className="flex gap-2">
          <label className="flex flex-1 flex-col gap-1">
            <span className="text-xs text-muted-foreground">Pages</span>
            <input
              className={field}
              value={form.pages}
              placeholder="Whole set"
              inputMode="numeric"
              onChange={(event) => setForm({ ...form, pages: event.target.value })}
            />
          </label>
          <label className="flex flex-1 flex-col gap-1">
            <span className="text-xs text-muted-foreground">Answer</span>
            <select
              className={field}
              value={form.shape}
              onChange={(event) => setForm({ ...form, shape: event.target.value as UnderstoodForm['shape'] })}
            >
              {SHAPE_CHOICES.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.words}
                </option>
              ))}
            </select>
          </label>
        </div>
        {problem && (
          <p id={`${formId}-problem`} role="alert" className="text-xs text-destructive">
            {problem}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => {
              setForm(null);
              setProblem(null);
            }}
            className="h-8 rounded-md px-3 text-xs text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={disabled}
            className="h-8 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50"
          >
            Resend
          </button>
        </div>
      </form>
    );
  }

  return (
    <p data-slot="chat-understood" className="-mt-2 max-w-[min(85%,36rem)] self-end text-right text-xs leading-relaxed text-muted-foreground">
      <span>Understood as: </span>
      <span>{understoodWords(understood)}</span>
      {canEdit && (
        <>
          <span aria-hidden="true"> · </span>
          <button
            type="button"
            onClick={() => setForm(formOf(understood))}
            aria-label={`Edit how the question was understood: ${understoodWords(understood)}`}
            className="rounded-sm underline-offset-2 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            Edit
          </button>
        </>
      )}
    </p>
  );
}
