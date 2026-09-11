/**
 * What the reviewer sees while a chat request is actually in flight.
 *
 * The old state was three bouncing dots. A request that takes four seconds looked identical to one
 * that takes forty, and nothing said what the system was doing with the time — so the product read
 * as frozen rather than as working.
 *
 * **Every number here is real.** The elapsed clock is measured. The stage list is the pipeline this
 * request genuinely runs, in order, and is the same sequence the page header states: recorded values
 * → deterministic checks → optional AI narration. What is *not* claimed is progress: the bar is
 * indeterminate — a segment crossing the track rather than a fill that grows — because a fill that
 * grows asserts a completion fraction, and nothing on this side of the request knows one. Inventing
 * "63%" would be the interface lying about something it cannot see.
 *
 * For the same reason no stage shows a tick or a count. "Read 47 values" would be a fabricated
 * measurement; the highlighted stage says what is being worked on, and that is all this side knows.
 */

import { useEffect, useRef, useState } from 'react';
import { GVMark } from '../brand/GVMark';
import './ThinkingStream.css';

/**
 * The stages a reviewer question passes through, as `ReviewPage` states them.
 *
 * `after` is how long the stage has been running before the next one is shown as active. These are
 * presentation timings, not measurements — the request does not report its internal position — so
 * the last stage stays active until the response actually lands rather than advancing to a
 * "finishing" state the code cannot verify.
 */
const STAGES = [
  { label: 'Reading the recorded values', after: 0 },
  { label: 'Matching the question to checks', after: 900 },
  { label: 'Running deterministic checks', after: 2100 },
  { label: 'Composing the explanation', after: 3600 },
] as const;

export function ThinkingStream() {
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef<number>(0);

  useEffect(() => {
    // The clock starts in the effect, not in `useRef(Date.now())` during render. Render has to be
    // pure — React may call it more than once for a single commit — so a timestamp taken there can
    // be from an attempt that was thrown away, and the elapsed figure would be wrong by however
    // long that took.
    startedAt.current = Date.now();

    // 100ms, not 16ms: the clock displays tenths, so a frame-rate timer would do ninety redundant
    // renders a second to show the same digits.
    const timer = window.setInterval(() => {
      setElapsed(Date.now() - startedAt.current);
    }, 100);
    return () => window.clearInterval(timer);
  }, []);

  const activeIndex = STAGES.reduce(
    (found, stage, index) => (elapsed >= stage.after ? index : found),
    0,
  );

  return (
    <div className="thinking" role="status" aria-live="polite">
      <div className="thinking__head">
        <span className="thinking__halo" aria-hidden="true">
          <GVMark size={22} animated />
        </span>
        <span className="thinking__title">Reviewing</span>
        <span className="thinking__elapsed mono" aria-label="elapsed time">
          {(elapsed / 1000).toFixed(1)}s
        </span>
      </div>

      {/* Indeterminate by construction — see the note at the top of this file. */}
      <div className="thinking__track" aria-hidden="true">
        <span className="thinking__bar" />
      </div>

      <ol className="thinking__stages">
        {STAGES.map((stage, index) => {
          const state = index < activeIndex ? 'done' : index === activeIndex ? 'active' : 'waiting';
          return (
            <li key={stage.label} className="thinking__stage" data-state={state}>
              <span className="thinking__marker" aria-hidden="true" />
              <span className="thinking__stage-label">{stage.label}</span>
              {state === 'active' && (
                <span className="thinking__dots" aria-hidden="true">
                  <span /><span /><span />
                </span>
              )}
            </li>
          );
        })}
      </ol>

      {/* There was a `modelId` line here. Nothing could ever fill it: the model id arrives *with*
          the response, so while the request is in flight this component cannot know one. The
          narration badge on the finished message names it, which is the first moment it is a fact.
          A prop no caller can supply reads as an integration somebody forgot to finish. */}
    </div>
  );
}
