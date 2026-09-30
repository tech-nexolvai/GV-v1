/**
 * What the screen shows while a model is deciding which reading fills which field.
 *
 * **Every number on this panel came from the server having done the thing.** The phases are the
 * five the request genuinely runs and each frame arrives as that phase begins; the percentage is
 * *phases finished*, which the endpoint knows because the sequence has a fixed length; the elapsed
 * clock is measured here. Nothing is on a timer.
 *
 * That distinction is why this component exists beside `ThinkingStream` rather than replacing it.
 * `ThinkingStream` is deliberately indeterminate — a chat request reports no internal position, so
 * a fill that grows would assert a completion fraction nothing measured. This request *does* report
 * its position, so a determinate bar is the honest rendering and an indeterminate one would be
 * throwing away a fact.
 *
 * **The retry is visible, and that is the point of showing phases at all.** When the deterministic
 * guard refuses a proposal the model is asked again with the guard's own sentence. The bar holds
 * where it is — the work that was rejected did not advance anything — and the reason is shown. A
 * reviewer watching that learns the thing worth knowing about this feature: the answer is checked.
 */

import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, CircleDashed, MinusCircle, RefreshCw, ShieldCheck } from 'lucide-react';
import { GVMark } from '../brand/GVMark';
import type { AssignmentStep, ProposedMeasurements } from '../../api/client';
import './AssignmentProgress.css';

export function AssignmentProgress({
  steps,
  result,
  error,
}: {
  /** Every phase frame received so far, in arrival order. */
  steps: AssignmentStep[];
  /** The finished proposal, once the result frame has landed. */
  result: ProposedMeasurements | null;
  /** A transport or server failure, in the server's own words where there is one. */
  error: string | null;
}) {
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef(0);
  const running = result === null && error === null;

  useEffect(() => {
    // Started in the effect, not during render: React may call render more than once for a single
    // commit, so a timestamp taken there can come from an attempt that was discarded.
    startedAt.current = Date.now();
    const timer = window.setInterval(() => setElapsed(Date.now() - startedAt.current), 100);
    return () => window.clearInterval(timer);
  }, []);

  const latest = steps.length > 0 ? steps[steps.length - 1] : null;
  const percent = result ? 100 : (latest?.percent ?? 0);

  // The phase labels are the server's, sent once on the first frame. Kept here rather than written
  // into this file because a local copy would be a second answer to "what are the phases", free to
  // disagree with the endpoint's the first time one is added.
  const labels = steps.find((step) => step.sequence.length > 0)?.sequence ?? [];

  // One row per phase, showing the *last* frame for that phase — a retry re-sends a phase, and the
  // second attempt is the current truth about it.
  const byIndex = new Map<number, AssignmentStep>();
  for (const step of steps) byIndex.set(step.index, step);
  const activeIndex = latest?.index ?? 0;
  const retried = steps.some((step) => step.attempt > 1);

  return (
    <section
      className="assign"
      role="status"
      aria-live="polite"
      aria-busy={running}
      data-state={error ? 'error' : result ? 'done' : 'running'}
    >
      <header className="assign__head">
        <span className="assign__halo" aria-hidden="true">
          <GVMark size={24} animated={running} />
        </span>
        <div className="assign__titles">
          <h3 className="assign__title">
            {error ? 'The fields were left for you' : result ? 'Filled' : 'Filling the measurements'}
          </h3>
          <p className="assign__subtitle">
            {latest && running
              ? latest.label
              : result
                ? `${result.fields_filled} of ${result.fields_total} fields filled from ${result.readings_attached} attached readings`
                : 'Nothing was changed. Type the values yourself, or try again.'}
          </p>
        </div>
        <div className="assign__percent">
          <span className="assign__percent-value mono">{percent}</span>
          <span className="assign__percent-unit">%</span>
        </div>
      </header>

      {/* Determinate, because the server reports its position. The width is set inline and the
          transition that animates it is in CSS, with the rest of the motion system. */}
      <div
        className="assign__track"
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="phases finished"
      >
        <span className="assign__bar" style={{ width: `${percent}%` }} />
      </div>

      <p className="assign__legend">
        {percent}% is <strong>phases finished</strong>, not confidence in the answer and not a
        guess at how long the model will take. {running && <span className="mono">{(elapsed / 1000).toFixed(1)}s</span>}
      </p>

      <ol className="assign__phases">
        {labels.map((label, position) => {
          const index = position + 1;
          const step = byIndex.get(index) ?? null;
          // **A phase nobody reached is skipped, not done.** A proposal nobody made is never
          // checked, and a tick beside "Checking that answer" would claim a check that did not run
          // — which is the one claim this panel exists to make honestly.
          const state =
            error && index === activeIndex
              ? 'error'
              : step === null && index < activeIndex
                ? 'skipped'
                : step !== null && (result || index < activeIndex)
                  ? 'done'
                  : index === activeIndex
                    ? 'active'
                    : 'waiting';
          return (
            <li className="assign__phase" key={label} data-state={state}>
              <span className="assign__marker" aria-hidden="true">
                {state === 'done' ? (
                  <CheckCircle2 size={13} />
                ) : state === 'active' ? (
                  <CircleDashed size={13} className="anim-spin" />
                ) : state === 'skipped' ? (
                  <MinusCircle size={13} />
                ) : (
                  <CircleDashed size={13} />
                )}
              </span>
              <span className="assign__phase-label">{label}</span>
              {state === 'skipped' ? (
                <span className="assign__phase-detail">not reached</span>
              ) : (
                step?.detail && <span className="assign__phase-detail">{step.detail}</span>
              )}
              {step && step.attempt > 1 && (
                <span className="assign__retry">
                  <RefreshCw size={11} aria-hidden="true" /> attempt {step.attempt}
                </span>
              )}
            </li>
          );
        })}
      </ol>

      {retried && (
        <p className="assign__note">
          <ShieldCheck size={13} aria-hidden="true" /> A deterministic check refused the first
          answer and the model was asked again with the reason. The bar did not move, because
          rejected work is not progress.
        </p>
      )}

      {result?.unfilled_reason && (
        <p className="assign__note assign__note--refused">
          <ShieldCheck size={13} aria-hidden="true" /> {result.unfilled_reason}. The fields stay
          empty for you, which is what happens without this step at all.
        </p>
      )}

      {error && <p className="assign__note assign__note--refused">{error}</p>}

      {result && result.model_id && (
        <p className="assign__model mono">
          proposed by {result.model_id} · checked by seven structural rules · saved by nobody yet
        </p>
      )}
    </section>
  );
}
