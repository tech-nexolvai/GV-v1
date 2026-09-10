import { useEffect, useState } from 'react';
import { CheckCircle2, AlertTriangle, ScanLine } from 'lucide-react';
import {
  ApiError,
  confirmCandidate,
  downloadCandidateCrop,
  listCandidates,
  listSemanticTypes,
  type CandidateOut,
} from '../api/client';
import { projectId } from '../api/config';
import './ConfirmReadingsPage.css';

/**
 * What the drawing said, and a reviewer saying what it means.
 *
 * This is the step that joins the two halves of the product. Extraction reads a drawing and stops at
 * untyped readings; the rules decide but need to know which quantity a number is. Until now the only
 * bridge was a person retyping the value into a form, which threw the machine's reading away.
 *
 * **The value and the crop are shown together, and that is not decoration.** Confirming records
 * `HUMAN_CONFIRMED`, which is a claim about the whole reading — that the value is what the drawing
 * says *and* that it is this quantity. A type picker on its own would be collecting a signature for
 * something nobody looked at.
 *
 * **Nothing here suggests a type.** The list is offered in the order the readings appear on the
 * sheet, with no ranking, no default selection and no "likely" marker. Guessing what a dimension is
 * from its position is the one thing gated on the real drawings (#274) and the vocabulary Q20 defers
 * — and a pre-selected dropdown is that guess wearing a reviewer's signature.
 *
 * **No arithmetic on a value, ever.** The value arrives as exact text and is displayed as it arrived.
 * JavaScript has no exact rational, and under exact match (Q2) there is no tolerance band to absorb a
 * rounding error, so a number this file reformatted could already be a different verdict.
 */

interface Props {
  packageId: string;
  onDone: () => void;
}

export function ConfirmReadingsPage({ packageId, onDone }: Props) {
  const [candidates, setCandidates] = useState<CandidateOut[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let live = true;
    let retry: number | undefined;
    const load = () => Promise.all([listCandidates(projectId(), packageId), listSemanticTypes()])
      .then(([read, vocabulary]) => {
        if (!live) return;
        setCandidates(read.candidates);
        setTypes(vocabulary);
        // The upload screen moves straight into this review step, while extraction is deliberately
        // asynchronous. Poll only an empty proposal list: once a reading arrives the UI is stable;
        // when none ever arrives the reviewer can immediately continue with manual values.
        if (read.candidates.length === 0) retry = window.setTimeout(load, 2_000);
      })
      .catch((cause: unknown) => {
        if (!live) return;
        setError(
          cause instanceof ApiError ? cause.message : 'The readings could not be loaded.',
        );
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    void load();
    return () => {
      live = false;
      if (retry !== undefined) window.clearTimeout(retry);
    };
  }, [packageId]);

  async function confirm(candidate: CandidateOut) {
    const semanticType = chosen[candidate.candidate_id];
    if (!semanticType) return;
    try {
      const result = await confirmCandidate(
        projectId(),
        packageId,
        candidate.candidate_id,
        semanticType,
      );
      setConfirmed((done) => ({ ...done, [candidate.candidate_id]: result.semantic_type }));
      setFailed((problems) => {
        const remaining = { ...problems };
        delete remaining[candidate.candidate_id];
        return remaining;
      });
    } catch (cause: unknown) {
      // Shown against the row rather than as a banner: a refusal is about this reading — already
      // confirmed, or a page read before its transform was recorded — and a message at the top of
      // the page would leave the reviewer hunting for which one it meant.
      setFailed((problems) => ({
        ...problems,
        [candidate.candidate_id]:
          cause instanceof ApiError ? cause.message : 'This reading could not be confirmed.',
      }));
    }
  }

  if (loading) return <p className="readings__status">Loading what was read…</p>;
  if (error) return <p className="readings__status readings__status--error">{error}</p>;

  if (candidates.length === 0) {
    return (
      <div className="readings">
        <h1 className="readings__title">Confirm what was read</h1>
        <p className="readings__status">
          Waiting for exact AI readings from the drawing. If none are proposed, the reviewer can
          continue now and supply only the values AI abstained on.
        </p>
        <button type="button" className="readings__done" onClick={onDone}>
          Continue with reviewer inputs
        </button>
      </div>
    );
  }

  const outstanding = candidates.filter((c) => !confirmed[c.candidate_id]).length;

  return (
    <div className="readings">
      <h1 className="readings__title">Confirm what was read</h1>
      <p className="readings__lede">
        AI proposed {candidates.length} exact reading{candidates.length === 1 ? '' : 's'} from the
        drawing. Check each mechanical crop, then say which quantity it is. AI did not choose a type;
        anything it did not propose stays for you to enter on the next screen.
      </p>

      <ul className="readings__list">
        {candidates.map((candidate) => {
          const done = confirmed[candidate.candidate_id];
          const problem = failed[candidate.candidate_id];
          return (
            <li
              key={candidate.candidate_id}
              className={`reading${done ? ' reading--confirmed' : ''}`}
            >
              <div className="reading__evidence">
                <span className="reading__page">p{candidate.page_index + 1}</span>
                <span className="reading__value">{candidate.value ?? '—'}</span>
                <span className="reading__raw" title="As printed on the drawing">
                  {candidate.raw_text}
                </span>
                {candidate.confidence && (
                  <span className="reading__confidence">
                    read confidence {candidate.confidence}
                  </span>
                )}
                {candidate.corroboration_status === 'CONFLICTING' && (
                  <span className="reading__conflict">
                    <AlertTriangle size={14} aria-hidden="true" />
                    The drawing's two readings of this disagree
                  </span>
                )}
              </div>

              <div className="reading__crop">
                {candidate.crop_key ? (
                  <ProposalCrop candidate={candidate} packageId={packageId} />
                ) : (
                  <span className="reading__crop-name reading__crop-name--absent">
                    no crop
                  </span>
                )}
              </div>

              {done ? (
                <p className="reading__done">
                  <CheckCircle2 size={16} aria-hidden="true" /> Confirmed as {done}
                </p>
              ) : (
                <div className="reading__action">
                  <label className="reading__label">
                    <span className="reading__label-text">This reading is</span>
                    <select
                      className="reading__select"
                      value={chosen[candidate.candidate_id] ?? ''}
                      onChange={(event) =>
                        setChosen((picked) => ({
                          ...picked,
                          [candidate.candidate_id]: event.target.value,
                        }))
                      }
                    >
                      {/* Empty and first, so nothing is chosen for the reviewer. */}
                      <option value="">Choose a quantity…</option>
                      {types.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="reading__confirm"
                    disabled={!chosen[candidate.candidate_id]}
                    onClick={() => void confirm(candidate)}
                  >
                    Confirm
                  </button>
                </div>
              )}

              {problem && <p className="reading__problem">{problem}</p>}
            </li>
          );
        })}
      </ul>

      <div className="readings__footer">
        <p className="readings__count">
          {outstanding === 0
            ? 'Every reading has been confirmed.'
            : `${outstanding} of ${candidates.length} still to confirm.`}
        </p>
        <button type="button" className="readings__done" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

function ProposalCrop({ candidate, packageId }: { candidate: CandidateOut; packageId: string }) {
  const [state, setState] = useState<{ url: string } | { error: string } | null>(null);

  useEffect(() => {
    let live = true;
    let objectUrl: string | null = null;
    void downloadCandidateCrop(projectId(), packageId, candidate.candidate_id).then(
      (blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (live) setState({ url: objectUrl });
        else URL.revokeObjectURL(objectUrl);
      },
      (cause: unknown) => {
        if (live) {
          setState({ error: cause instanceof Error ? cause.message : 'The stored crop could not be loaded.' });
        }
      },
    );
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [candidate.candidate_id, packageId]);

  if (state && 'error' in state) return <span className="reading__crop-name reading__crop-name--absent">crop unavailable</span>;
  if (!state || !('url' in state)) return <span className="reading__crop-name"><ScanLine size={14} aria-hidden="true" /> loading crop…</span>;
  return <img className="reading__crop-image" src={state.url} alt={`Mechanical crop for ${candidate.raw_text} on page ${candidate.page_index + 1}`} />;
}
