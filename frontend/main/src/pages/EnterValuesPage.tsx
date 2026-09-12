import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  ChevronRight,
  Play,
  Plus,
  ScanLine,
  Sparkles,
  Trash2,
} from 'lucide-react';
import {
  ApiError,
  confirmCandidate,
  downloadCandidateCrop,
  enterMeasurements,
  getRequiredInputs,
  listCandidates,
  listSemanticTypes,
  proposeMeasurements,
  requestChecks,
  type AssignmentStep,
  type CandidateOut,
  type ProposedMeasurements,
} from '../api/client';
import { projectId } from '../api/config';
import { AssignmentProgress } from '../components/measure/AssignmentProgress';
import './EnterValuesPage.css';

/**
 * The reviewer completes the dimensions, and the deterministic engine decides.
 *
 * `CLIENT_FACTS` Q7: *"the reviewer types the values into input fields for that drawing set"*. The
 * reviewer still owns the final input: an AI proposal becomes usable only after that person has
 * inspected its mechanical crop and selected its meaning. Confirmed readings are then placed in the
 * same editable fields as reviewer-read values; the reviewer may correct them before saving.
 *
 * **Every field comes from the server, and that is what makes the form complete.** A list of fields
 * written here would be right today and silently wrong the first time a rule gained an input — the
 * check would then abstain for a reason the reviewer could not act on, indistinguishable from a
 * genuine missing dimension. `GET .../required-inputs` derives the fields from the published rules,
 * so a rule that gains an input gains a field.
 *
 * AI proposals live here with the rule inputs they can populate. A reviewer picks a meaning only
 * after seeing the mechanical crop; that one explicit choice saves the confirmation and fills the
 * matching field. The reviewer may edit it before saving. **Nothing on this page does arithmetic
 * on a value.** The strings go to the server exactly as typed
 * and are parsed there by the same code that reads a drawing. JavaScript has no exact rational, and
 * under exact match (Q2) there is no tolerance band to absorb a rounding error, so a number this file
 * converted could already be a different verdict.
 */

type Quantity = {
  key: string;
  semantic_type: string;
  source: string;
  many: boolean;
  consumers: { rule_id?: string; input_name?: string }[];
};
type Parameter = {
  name: string;
  scope: string;
  rule_ids: string[];
  declared_default: string | null;
  blocked: boolean;
};
type Discriminator = { name: string; rule_ids: string[]; choices: string[] };
type ConfirmedReading = {
  key: string;
  source: string;
  semantic_type: string;
  value: string;
  qualification: 'reviewer_confirmed' | 'exact_vector_tag';
};

/** One entry on the wire. Exactly one of `value` or `values`, which the server also enforces. */
type MeasurementEntry = {
  rule_id: string;
  name: string;
  value?: string;
  values?: string[];
};
type Needed = {
  quantities: Quantity[];
  confirmed_readings: ConfirmedReading[];
  parameters: Parameter[];
  discriminators: Discriminator[];
  rules_published: number;
};

/** Which sheet a measurement is read from, in the words a reviewer uses. */
const SOURCE_LABEL: Record<string, string> = {
  SHOP: 'shop drawing',
  ARCH: 'architectural drawing',
  USER_INPUT: 'measured on site',
  PRODUCT_SPEC: 'product specification',
};

/**
 * Who put the value that is currently in a field, said in four words.
 *
 * **A proposal must never look like a confirmation.** The three sources reach the same box — a
 * reviewer's typing, a reading they confirmed on the crop, and a model's proposal — and once they
 * are in it nothing distinguishes them. That is the failure worth designing against: a number a
 * model chose, signed off because it looked like one a person had already checked.
 */
const ORIGIN_LABEL: Record<string, string> = {
  empty: 'needs a value',
  proposed: 'proposed by AI — check it',
  confirmed: 'you confirmed this reading',
  tagged: 'exact drawing tag',
  typed: 'you typed this',
};

/**
 * What to call this quantity in front of a person.
 *
 * The field was labelled `CT004`, which is the semantic type — a code that means something precise
 * to the rulebook and nothing at all to a reviewer looking at a form. The rulebook already names
 * the same quantity readably: `CT004` is `sink_cabinet_width` where `CT-SINK-CABINET-WIDTH-001`
 * consumes it, and the API sends that `input_name` with every quantity.
 *
 * So the label is the rulebook's own word, not one invented here. Where two rules name the same
 * quantity differently — `CT008` is `cutout_depth` to one check and `sink_depth` to another — the
 * first is used and the code stays beside it, because the code is what both rules actually agree on
 * and what a reviewer would quote when asking about it.
 */
function fieldLabel(quantity: Quantity): string {
  const named = quantity.consumers.find((consumer) => consumer.input_name)?.input_name;
  if (!named) return quantity.semantic_type;
  const words = named.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function EnterValuesPage({
  packageId: selectedPackageId,
  onDone,
  onChoosePackage,
}: {
  packageId?: string;
  onDone?: (packageId: string) => void;
  onChoosePackage?: () => void;
}) {
  const [needed, setNeeded] = useState<Needed | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [packageId, setPackageId] = useState<string | null>(null);
  /** Single-valued quantities and parameters, keyed by quantity key or parameter name. */
  const [singles, setSingles] = useState<Record<string, string>>({});
  const [reviewerEditedSingles, setReviewerEditedSingles] = useState<Set<string>>(() => new Set());
  /** Many-valued quantities, in layout order. */
  const [runs, setRuns] = useState<Record<string, string[]>>({});
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [stored, setStored] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidateOut[]>([]);
  const [semanticTypes, setSemanticTypes] = useState<string[]>([]);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  /** The phases of an assignment in flight, in arrival order. Empty until one is asked for. */
  const [proposalSteps, setProposalSteps] = useState<AssignmentStep[]>([]);
  const [proposal, setProposal] = useState<ProposedMeasurements | null>(null);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  /** Whether the crop-inspection list is open. Closed by default; it is the slow route. */
  const [inspecting, setInspecting] = useState(false);
  /**
   * Which fields a model proposed, by field key, with the readings it chose.
   *
   * **Nothing a model proposed reaches a reviewer unmarked.** A value that appeared in a box with
   * no explanation is indistinguishable from one a person read off the drawing, and the reviewer's
   * confirmation is the only thing standing between a proposal and a verdict. The mark is dropped
   * the moment they type in the field: it is theirs from then on.
   */
  const [aiFilled, setAiFilled] = useState<Record<string, string[]>>({});

  useEffect(() => {
    let cancelled = false;
    // A package switch must never leave the prior package's fields enabled while the new contract is
    // loading. The reviewer could otherwise submit a value against the wrong drawing pair.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPackageId('');
    setNeeded(null);
    setRuns({});
    setSingles({});
    setReviewerEditedSingles(new Set());
    setChoices({});
    setCandidates([]);
    setSemanticTypes([]);
    setCandidateError(null);
    setLoadError(null);
    setProposalSteps([]);
    setProposal(null);
    setProposalError(null);
    setAiFilled({});
    (async () => {
      try {
        if (!selectedPackageId) return;
        const [fields, read, vocabulary] = await Promise.all([
          getRequiredInputs(projectId(), selectedPackageId),
          listCandidates(projectId(), selectedPackageId),
          listSemanticTypes(),
        ]);
        if (cancelled) return;
        const required = fields as unknown as Needed;
        setPackageId(selectedPackageId);
        setNeeded(required);
        setCandidates(read.candidates);
        setSemanticTypes(vocabulary);
        const confirmedByKey = required.confirmed_readings.reduce<Record<string, string[]>>(
          (grouped, reading) => ({
            ...grouped,
            [reading.key]: [...(grouped[reading.key] ?? []), reading.value],
          }),
          {},
        );
        const prefilledSingles = Object.fromEntries(
          required.quantities
            .filter((q) => !q.many)
            // A scalar with two confirmed readings is a real ambiguity.  Leave it empty for the
            // reviewer instead of choosing the first result based on insertion order.
            .map(
              (q): [string, string] => [
                q.key,
                (confirmedByKey[q.key] ?? []).length === 1 ? confirmedByKey[q.key][0] : '',
              ],
            ),
        );
        const prefilledRuns = Object.fromEntries(
          required.quantities
            .filter((q) => q.many)
            .map((q) => [
              q.key,
              confirmedByKey[q.key]?.filter((value) => value.trim())?.length
                ? confirmedByKey[q.key].filter((value) => value.trim())
                : [''],
            ]) as Array<[string, string[]]>,
        );
        setSingles((prior) => ({ ...prior, ...prefilledSingles }));
        setRuns(
          Object.fromEntries(
            required.quantities.filter((q) => q.many).map((q) => [q.key, ['']]),
          ),
        );
        if (Object.keys(prefilledRuns).length > 0) {
          setRuns((prior) => ({ ...prior, ...prefilledRuns }));
        }
      } catch (caught) {
        if (!cancelled) {
          setLoadError(caught instanceof ApiError ? caught.message : String(caught));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Re-fetch only when the selected package changes, never on a keystroke within its form.
  }, [selectedPackageId]);

  if (!selectedPackageId) {
    return (
      <div className="enter-values">
        <h1>Choose a review first</h1>
        <p className="enter-values__hint">
          Measurements belong to one uploaded drawing pair. Open that pair from Documents, then
          confirm any AI readings and continue here.
        </p>
        {onChoosePackage && (
          <button type="button" className="value-primary" onClick={onChoosePackage}>
            Open documents
          </button>
        )}
      </div>
    );
  }

  const setRun = (key: string, index: number, value: string) =>
    setRuns((prior) => ({
      ...prior,
      [key]: (prior[key] ?? ['']).map((v, i) => (i === index ? value : v)),
    }));

  async function confirmFromMeasure(candidate: CandidateOut, semanticType: string) {
    if (!packageId || !needed || !candidate.source || !candidate.value) return;
    const target = needed.quantities.find(
      (quantity) => quantity.key === `${candidate.source}:${semanticType}`,
    );
    if (!target) {
      setCandidateError(
        `${semanticType} is not a measurement this published rulebook asks for from this document.`,
      );
      return;
    }

    setConfirming(candidate.candidate_id);
    setCandidateError(null);
    try {
      const result = await confirmCandidate(
        projectId(),
        packageId,
        candidate.candidate_id,
        semanticType,
      );
      const key = `${candidate.source}:${result.semantic_type}`;
      const reading: ConfirmedReading = {
        key,
        source: candidate.source,
        semantic_type: result.semantic_type,
        value: candidate.value,
        qualification: 'reviewer_confirmed',
      };

      // The server commits the human confirmation before the field changes. A browser reload cannot
      // lose it, and this UI is only reflecting that persisted fact.
      setNeeded((current) =>
        current === null
          ? current
          : { ...current, confirmed_readings: [...current.confirmed_readings, reading] },
      );
      if (target.many) {
        setRuns((prior) => {
          const current = (prior[target.key] ?? []).filter((value) => value.trim());
          return {
            ...prior,
            [target.key]: current.includes(reading.value) ? current : [...current, reading.value],
          };
        });
      } else {
        // A reviewer-entered value has priority in the editable form; do not erase it behind their
        // back merely because they later confirm an AI proposal.
      setSingles((prior) => {
        const readingCount = (needed.confirmed_readings.filter((item) => item.key === key).length) + 1;
        if (reviewerEditedSingles.has(target.key)) return prior;
        return { ...prior, [target.key]: readingCount === 1 ? reading.value : '' };
      });
      }
      setCandidates((current) =>
        current.filter((item) => item.candidate_id !== candidate.candidate_id),
      );
    } catch (caught) {
      setCandidateError(
        caught instanceof ApiError ? caught.message : 'This AI reading could not be confirmed.',
      );
    } finally {
      setConfirming(null);
    }
  }

  function typesForCandidate(candidate: CandidateOut): string[] {
    if (!candidate.source || !needed) return [];
    const requiredForSource = new Set(
      needed.quantities
        .filter((quantity) => quantity.source === candidate.source)
        .map((quantity) => quantity.semantic_type),
    );
    return semanticTypes.filter((semanticType) => requiredForSource.has(semanticType));
  }

  /**
   * Persist exactly what is currently visible in the form.
   *
   * This is intentionally shared by Save and Run checks.  A reviewer who confirms an AI reading sees
   * it fill the field, then reasonably expects Run checks to use that field.  Queuing first would
   * create a run without the displayed measurements, which is both surprising and unsafe.
   */
  async function saveVisibleValues(): Promise<boolean> {
    if (!packageId || !needed) return false;
    try {
      // **One typed value fans out to every rule input it feeds.** The mapping is the server's, taken
      // from `consumers` — a reviewer measures the front offset once, and three rules receive it.
      const measurements = needed.quantities.flatMap<MeasurementEntry>((quantity) => {
        const consumers = quantity.consumers.filter((c) => c.rule_id && c.input_name);
        if (quantity.many) {
          const values = (runs[quantity.key] ?? []).map((v) => v.trim()).filter(Boolean);
          if (!values.length) return [];
          return consumers.map((c) => ({
            rule_id: c.rule_id as string,
            name: c.input_name as string,
            values,
          }));
        }
        const value = (singles[quantity.key] ?? '').trim();
        if (!value) return [];
        return consumers.map((c) => ({
          rule_id: c.rule_id as string,
          name: c.input_name as string,
          value,
        }));
      });

      const parameters = needed.parameters
        .filter((p) => !p.blocked && (singles[p.name] ?? '').trim())
        .map((p) => ({
          name: p.name,
          value: (singles[p.name] ?? '').trim(),
          scope: (p.scope === 'run' ? 'run' : 'project') as 'run' | 'project',
        }));

      const result = await enterMeasurements(projectId(), packageId, { parameters, measurements });
      // Echo the parse, not the input: `25.5"` and `25 1/2"` are the same value and a reviewer should
      // see that the system agrees — which is also how a mistyped unit becomes visible.
      setStored([
        ...result.parameters.map((v) => `${v.name} = ${v.numerator}/${v.denominator} ${v.unit}`),
        ...result.measurements.map((v) => `${v.name} = ${v.numerator}/${v.denominator} ${v.unit}`),
        ...(result.lists ?? []).map(
          (l) => `${l.name} = [${l.values.map((v) => `${v.numerator}/${v.denominator}`).join(', ')}]`,
        ),
      ]);
      return true;
    } catch (caught) {
      // The server's own message, verbatim. It names the field and says what to do about it; a
      // rewritten "invalid input" would lose both.
      setError(caught instanceof ApiError ? caught.message : String(caught));
      return false;
    }
  }

  /**
   * Ask which reading fills which field, and put the accepted answers in the boxes.
   *
   * **What arrives has already survived every check that can be made without reading a number.**
   * `workflow/assignment.py` refuses a reading from the wrong sheet, a reading attached to no
   * dimension line, one reading claimed by two fields, several readings for a field that takes one,
   * and a run gathered from two different places on the drawing. A proposal that fails any of them
   * is refused whole, and the fields stay empty — which is exactly what happens without this step.
   *
   * **It never overwrites the reviewer.** A field they have typed in, and a field already filled by
   * a reading they confirmed on the crop, are both left alone. A model's proposal is the weakest
   * claim on this screen and it yields to both.
   */
  async function onPropose() {
    if (!packageId || !needed) return;
    setProposing(true);
    setProposalSteps([]);
    setProposal(null);
    setProposalError(null);
    try {
      const result = await proposeMeasurements(projectId(), packageId, (step) =>
        setProposalSteps((prior) => [...prior, step]),
      );
      setProposal(result);

      const filledSingles: Record<string, string> = {};
      const filledRuns: Record<string, string[]> = {};
      const marks: Record<string, string[]> = {};
      for (const assignment of result.assignments) {
        const values = assignment.values.map((reading) => reading.value);
        if (assignment.many) {
          const current = (runs[assignment.field_key] ?? []).filter((value) => value.trim());
          if (current.length > 0) continue;
          filledRuns[assignment.field_key] = values;
        } else {
          if (reviewerEditedSingles.has(assignment.field_key)) continue;
          if ((singles[assignment.field_key] ?? '').trim()) continue;
          filledSingles[assignment.field_key] = values[0] ?? '';
        }
        marks[assignment.field_key] = assignment.values.map((reading) => reading.candidate_id);
      }
      setSingles((prior) => ({ ...prior, ...filledSingles }));
      setRuns((prior) => ({ ...prior, ...filledRuns }));
      setAiFilled(marks);
    } catch (caught) {
      setProposalError(
        caught instanceof ApiError ? caught.message : 'The proposal could not be requested.',
      );
    } finally {
      setProposing(false);
    }
  }

  /** A reviewer typing in a field makes it theirs, so the proposal mark comes off. */
  function releaseField(key: string) {
    setAiFilled((prior) => {
      if (!(key in prior)) return prior;
      const next = { ...prior };
      delete next[key];
      return next;
    });
  }

  async function onSave() {
    if (!packageId || !needed) return;
    setBusy(true);
    setError(null);
    setAccepted(null);
    try {
      await saveVisibleValues();
    } finally {
      setBusy(false);
    }
  }

  async function onRunChecks() {
    if (!packageId || !needed) return;
    setBusy(true);
    setError(null);
    setAccepted(null);
    try {
      // A run must evaluate the values the reviewer can see, including any just-confirmed AI
      // proposals.  If parsing or storage fails, do not enqueue a stale or empty measurement set.
      if (!(await saveVisibleValues())) return;
      const response = await requestChecks(projectId(), packageId, choices);
      setAccepted(response.accepted_id);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <div className="enter-values">
        <div className="enter-values__error" role="alert">
          {loadError}
        </div>
      </div>
    );
  }
  if (!needed) {
    return (
      <div className="enter-values">
        <p className="enter-values__hint">Reading what the rulebook needs…</p>
      </div>
    );
  }

  const readingsByKey = needed.confirmed_readings.reduce<Record<string, ConfirmedReading[]>>(
    (grouped, reading) => ({ ...grouped, [reading.key]: [...(grouped[reading.key] ?? []), reading] }),
    {},
  );
  // A scalar is only prefilled when it has one unambiguous qualified reading.  Counting two
  // conflicting readings as an "AI-filled field" would be a confidence claim the UI cannot make.
  const exactTagFieldCount = needed.quantities.filter((quantity) =>
    (readingsByKey[quantity.key] ?? []).some((reading) => reading.qualification === 'exact_vector_tag'),
  ).length;
  //: How many drawing readings a reviewer has already given a meaning to. Counted from the
  //: confirmed readings themselves rather than from filled fields: one reading can feed several
  //: rules, and counting fields would report the same confirmation more than once.
  const confirmedCount = needed.confirmed_readings.length;
  const measurementFieldCount = needed.quantities.length;

  /** Whether a field currently holds anything, from whatever source. The honest coverage figure. */
  const hasValue = (quantity: Quantity): boolean =>
    quantity.many
      ? (runs[quantity.key] ?? []).some((value) => value.trim().length > 0)
      : (singles[quantity.key] ?? '').trim().length > 0;
  const filledFieldCount = needed.quantities.filter(hasValue).length;
  const coveragePercent =
    measurementFieldCount === 0
      ? 0
      : Math.round((filledFieldCount / measurementFieldCount) * 100);

  /**
   * Where a field's current value came from, in the order that outranks.
   *
   * A reviewer's own typing beats a reading they confirmed on the crop, which beats a model's
   * proposal. The pill says which, because "who put this number here" is the question a reviewer
   * has to be able to answer before they sign the form — and three sources that look identical in
   * a box is most of what makes this screen hard to trust.
   */
  const fieldOrigin = (
    quantity: Quantity,
  ): 'empty' | 'proposed' | 'confirmed' | 'tagged' | 'typed' => {
    if (!hasValue(quantity)) return 'empty';
    if (quantity.key in aiFilled) return 'proposed';
    const readings = readingsByKey[quantity.key] ?? [];
    if (readings.some((reading) => reading.qualification === 'exact_vector_tag')) return 'tagged';
    if (readings.length > 0) return 'confirmed';
    return 'typed';
  };

  /** Whether a proposal has been asked for at all. What decides who owns the panel's space. */
  const attempted = proposing || proposalSteps.length > 0 || proposal !== null || proposalError !== null;

  /** The sheets this rulebook reads from, in the order the fields appear. One group per sheet. */
  const sheets = needed.quantities.reduce<string[]>(
    (seen, quantity) => (seen.includes(quantity.source) ? seen : [...seen, quantity.source]),
    [],
  );

  return (
    <div className="enter-values">
      {/* **Two sentences, not five.** Everything here was true and none of it was what a reviewer
          opening the page needs first, which is the format of a value. The rest is the rationale
          for the form's existence — worth saying once, in small type, under the instruction. */}
      <header className="enter-values__head">
        <h1>Enter measurements</h1>
        <p>
          Type each value with its unit — <code>25 1/2&quot;</code> or <code>648 mm</code>. A value
          with no unit is refused rather than guessed at.
        </p>
        <p className="enter-values__hint">
          Parsed exactly and compared by the rule engine; nothing is rounded and nothing is
          inferred. Every field below comes from the {needed.rules_published} published rules, so a
          check can only fail to decide for a reason you can see — never because a field was
          missing.
        </p>
      </header>

      <section className="enter-values__section">
        <h2>Measurements</h2>

        {/* **One line, one bar, three counts.**
            This was five stat tiles and a paragraph, and a reviewer opening the page could not tell
            at a glance whether there was anything to do. The bar is the only number that answers
            that question — how much of the form is filled — and it is labelled as coverage, because
            a filled field is not a right one and the screen must not imply that it is. */}
        <div className="measure-coverage" aria-label="How much of the form is filled">
          <div className="measure-coverage__head">
            <p className="measure-coverage__headline">
              <strong>{filledFieldCount}</strong> of <strong>{measurementFieldCount}</strong> fields
              have a value
            </p>
            <span className="measure-coverage__percent mono">{coveragePercent}%</span>
          </div>
          <div
            className="measure-coverage__track"
            role="progressbar"
            aria-valuenow={coveragePercent}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <span className="measure-coverage__bar" style={{ width: `${coveragePercent}%` }} />
          </div>
          <ul className="measure-coverage__counts">
            <li>
              <strong>{candidates.length + confirmedCount}</strong> dimensions read off the drawings
            </li>
            <li>
              <strong>{confirmedCount}</strong> confirmed by a reviewer
            </li>
            <li>
              <strong>{candidates.length}</strong> not yet given a meaning
            </li>
            {exactTagFieldCount > 0 && (
              <li>
                <strong>{exactTagFieldCount}</strong> filled with no click — the drawing states the
                meaning itself
              </li>
            )}
          </ul>
          <p className="measure-coverage__caveat">
            Coverage, not accuracy. A dimension the reader could not parse, or a number with no unit,
            is not counted here at all — it was refused rather than guessed, and the field stays
            empty for you.
          </p>
        </div>

        {/* **Filling the form from the drawings.**
            The model is not asked what a number means in the abstract; it is asked to map readings
            this pipeline has already located onto the fields the rulebook already names, and every
            structural property of a right answer is one `workflow/assignment.py` verifies. What it
            is never given is the rule arithmetic — a model that knew the equation could choose
            readings that make it balance, and the check would then confirm the balance on a drawing
            with a real error in it. */}
        {/* **The offer stands until an attempt has been made, and then the panel owns the space.**
            This was keyed on `proposalSteps.length === 0`, which is the state a request that failed
            *before its first frame* also lands in — a 413, a 404, a server that did not answer. The
            offer panel came back, `proposalError` was rendered nowhere, and pressing Fill with AI
            looked like pressing a button that does nothing. Keyed on whether anything was attempted
            instead, so a failure is shown rather than swallowed. */}
        {!attempted ? (
          <div className="measure-fill">
            <div className="measure-fill__text">
              <h3>Fill these from the drawings</h3>
              <p>
                A model proposes which reading fills which field. Every proposal is then checked
                against the drawing — the right sheet, attached to a real dimension line, one reading
                per field, and a run in the order the drawing draws it — and refused as a batch if
                any part of it fails. Nothing is saved until you press Save.
              </p>
            </div>
            <button
              type="button"
              className="value-primary interactive"
              onClick={() => void onPropose()}
              disabled={busy || candidates.length === 0}
            >
              <Sparkles size={14} aria-hidden="true" /> Fill with AI
            </button>
            {/* **Two situations, not one sentence covering both.**
                "every reading already has a meaning, or the reader found none" made the reviewer
                work out which of the two they were in — from a panel that already knows. One of
                them is a finished package and the other is a drawing nothing was read from, and
                they want completely different things done about them. */}
            {candidates.length === 0 && (
              <p className="enter-values__hint enter-values__hint--tight">
                {confirmedCount > 0
                  ? 'Nothing left to propose — every reading off these drawings already has a meaning.'
                  : 'Nothing was read off these drawings, so there is nothing to propose from. ' +
                    'Check on Documents that both drawings finished uploading and that the AI ' +
                    'reading has run.'}
              </p>
            )}
          </div>
        ) : (
          <AssignmentProgress
            steps={proposalSteps}
            result={proposal}
            error={proposalError}
          />
        )}
        {(proposal || proposalError) && !proposing && (
          <button
            type="button"
            className="value-secondary measure-fill__again"
            onClick={() => void onPropose()}
          >
            <Sparkles size={13} aria-hidden="true" /> Ask again
          </button>
        )}
        {/* **Collapsed, because it is the slow route and no longer the only one.**
            Every reading here is also offered beside the field it could fill, and now a model
            proposes which field that is. What this list is still for is the moment a reviewer
            wants to see the crop — the region of the uploaded PDF the number was read from —
            before saying what it means. Open, it pushed the form itself below the fold on every
            package with readings left. */}
        {candidates.length > 0 && (
          <section className="ai-proposals" aria-labelledby="ai-proposals-heading">
            <button
              type="button"
              className="ai-proposals__toggle"
              aria-expanded={inspecting}
              id="ai-proposals-heading"
              onClick={() => setInspecting((shown) => !shown)}
            >
              <ChevronRight
                size={14}
                className="collapsible-chevron"
                data-open={inspecting}
                aria-hidden="true"
              />
              Inspect {candidates.length} reading{candidates.length === 1 ? '' : 's'} on the crop
              before using {candidates.length === 1 ? 'it' : 'them'}
            </button>
            <div className="collapsible" data-open={inspecting} inert={!inspecting}>
            <div>
            {candidates.map((candidate) => {
              const availableTypes = typesForCandidate(candidate);
              const isConfirming = confirming === candidate.candidate_id;
              return (
                <div className="ai-proposal" key={candidate.candidate_id}>
                  <div className="ai-proposal__facts">
                    <strong>{candidate.value}</strong>
                    <span>{SOURCE_LABEL[candidate.source ?? ''] ?? 'drawing source unavailable'}</span>
                    <span>p{candidate.page_index + 1}</span>
                    {candidate.confidence && <span>read confidence {candidate.confidence}</span>}
                  </div>
                  {candidate.crop_key && packageId ? (
                    <MeasureCandidateCrop candidate={candidate} packageId={packageId} />
                  ) : (
                    <p className="ai-proposal__no-crop">
                      No crop is available, so this reading cannot be confirmed here.
                    </p>
                  )}
                  <label className="ai-proposal__type">
                    <span>Meaning / rule quantity</span>
                    <select
                      className="value-input"
                      aria-label={`Meaning for AI reading ${candidate.value}`}
                      defaultValue=""
                      disabled={isConfirming || !candidate.crop_key || availableTypes.length === 0}
                      onChange={(event) => {
                        const type = event.target.value;
                        if (type) void confirmFromMeasure(candidate, type);
                      }}
                    >
                      <option value="">Choose a quantity…</option>
                      {availableTypes.map((semanticType) => (
                        <option key={semanticType} value={semanticType}>
                          {semanticType}
                        </option>
                      ))}
                    </select>
                  </label>
                  {isConfirming && <span className="ai-proposal__saving">Saving confirmation…</span>}
                </div>
              );
            })}
            </div>
            </div>
            {candidateError && <p className="enter-values__error">{candidateError}</p>}
          </section>
        )}
        {needed.confirmed_readings.length > 0 ? (
          <p className="enter-values__hint" role="status">
            {needed.confirmed_readings.length} drawing reading{needed.confirmed_readings.length === 1 ? ' has' : 's have'} filled below.
            Review or edit them before saving.
          </p>
        ) : (
          <p className="enter-values__hint" role="status">
            No AI readings have been confirmed yet. Choose a meaning above, or enter a reviewer-read
            value below.
          </p>
        )}
        {/* **Grouped by the sheet the value is read from.**
            A flat list of fourteen fields asked the reviewer to jump between two drawings on every
            row. Grouped, they fill the shop drawing's fields with the shop drawing open, which is
            how the work is actually done — and the sheet is stated once as a heading instead of
            repeated fourteen times as a label. */}
        {sheets.map((sheet) => {
          const inSheet = needed.quantities.filter((quantity) => quantity.source === sheet);
          const done = inSheet.filter(hasValue).length;
          return (
            <div className="sheet-group" key={sheet}>
              <div className="sheet-group__head">
                <h3 className="sheet-group__title">
                  From the {SOURCE_LABEL[sheet] ?? sheet}
                </h3>
                <span className="sheet-group__count mono">
                  {done}/{inSheet.length}
                </span>
              </div>
              {inSheet.map((quantity) => {
                const origin = fieldOrigin(quantity);
                return (
                  <div className="value-field" key={quantity.key} data-origin={origin}>
                    <label className="value-label" htmlFor={`q-${quantity.key}`}>
                      {/* The rulebook's readable name leads; the code follows it. A reviewer filling
                          this in needs to know it is the sink cabinet width — `CT004` is what they
                          quote back when asking about it, not what tells them which box to type in. */}
                      <span className="value-name">{fieldLabel(quantity)}</span>
                      <span className="value-code">{quantity.semantic_type}</span>
                      <span className={`value-origin value-origin--${origin}`}>
                        {ORIGIN_LABEL[origin]}
                      </span>
                      <span className="value-feeds" title={quantity.consumers.map((c) => c.rule_id).join(', ')}>
                        {quantity.consumers.length} check{quantity.consumers.length === 1 ? '' : 's'}
                      </span>
                    </label>
                    {/* **The AI's own readings, offered at the field that wants one.**
                        They were previously listed in a separate block above the form: you picked a
                        meaning from a dropdown of raw codes up there, and the value appeared in a
                        box somewhere below. That is the interaction inside-out — it asks "what does
                        this number mean?" when the reviewer is looking at a field and asking "what
                        goes in here?". Offered here, confirming a reading is one click at the point
                        it is needed, and the meaning is the field it was clicked under rather than a
                        code chosen from a list. Nothing is filled in automatically: the click is the
                        reviewer saying what the number means. */}
                    <AiReadings
                      quantity={quantity}
                      candidates={candidates}
                      busyId={confirming}
                      onUse={(candidate) => void confirmFromMeasure(candidate, quantity.semantic_type)}
                    />
                    {quantity.many ? (
                      <>
                        {(runs[quantity.key] ?? ['']).map((value, index) => (
                          <div className="value-row" key={index}>
                            <input
                              className="value-input value-input--wide"
                              id={index === 0 ? `q-${quantity.key}` : undefined}
                              aria-label={`${fieldLabel(quantity)}, item ${index + 1}, left to right`}
                              placeholder={'25 1/2" or 648 mm'}
                              value={value}
                              onChange={(e) => {
                                releaseField(quantity.key);
                                setRun(quantity.key, index, e.target.value);
                              }}
                            />
                            <button
                              type="button"
                              className="value-remove interactive"
                              aria-label={`Remove item ${index + 1} from ${fieldLabel(quantity)}`}
                              onClick={() => {
                                releaseField(quantity.key);
                                setRuns((prior) => ({
                                  ...prior,
                                  [quantity.key]: (prior[quantity.key] ?? []).filter(
                                    (_, i) => i !== index,
                                  ),
                                }));
                              }}
                            >
                              <Trash2 size={14} aria-hidden="true" />
                            </button>
                          </div>
                        ))}
                        <button
                          type="button"
                          className="value-add interactive"
                          onClick={() =>
                            setRuns((prior) => ({
                              ...prior,
                              [quantity.key]: [...(prior[quantity.key] ?? []), ''],
                            }))
                          }
                        >
                          <Plus size={14} aria-hidden="true" /> Add another
                        </button>
                        <p className="enter-values__hint enter-values__hint--tight">
                          In order, left to right — two runs are compared position by position.
                        </p>
                      </>
                    ) : (
                      <input
                        className="value-input value-input--wide"
                        id={`q-${quantity.key}`}
                        placeholder={'25 1/2" or 648 mm'}
                        value={singles[quantity.key] ?? ''}
                        onChange={(e) => {
                          releaseField(quantity.key);
                          setReviewerEditedSingles((prior) => new Set(prior).add(quantity.key));
                          setSingles((prior) => ({ ...prior, [quantity.key]: e.target.value }));
                        }}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
      </section>

      <section className="enter-values__section">
        <h2>Settings</h2>
        <p className="enter-values__hint">
          Values for this job rather than dimensions off a drawing. Where the rulebook suggests one it
          is shown — a rule author&apos;s stand-in, not a number the client has confirmed.
        </p>
        {needed.parameters.map((parameter) => (
          <div className="value-field" key={parameter.name}>
            <label className="value-label" htmlFor={`p-${parameter.name}`}>
              {parameter.name}
              <span className="value-source">
                {parameter.scope === 'run' ? 'this review only' : 'this project'}
              </span>
              <span className="value-feeds">{parameter.rule_ids.join(', ')}</span>
            </label>
            {parameter.blocked ? (
              <p className="value-blocked" role="note">
                <AlertTriangle size={14} aria-hidden="true" /> Waiting on the vendor. This check will
                report that it could not decide, which is the correct answer until the value arrives —
                it is not a field to fill in.
              </p>
            ) : (
              <>
                <input
                  className="value-input value-input--wide"
                  id={`p-${parameter.name}`}
                  placeholder=""
                  value={singles[parameter.name] ?? ''}
                  onChange={(e) =>
                    setSingles((prior) => ({ ...prior, [parameter.name]: e.target.value }))
                  }
                />
                {parameter.declared_default && (
                  <p className="enter-values__hint enter-values__hint--tight">
                    The rulebook suggests <code>{parameter.declared_default}</code>. Leave blank to use
                    it, or type the value this job actually uses.
                  </p>
                )}
              </>
            )}
          </div>
        ))}
      </section>

      {needed.discriminators.length > 0 && (
        <section className="enter-values__section">
          <h2>Layout</h2>
          <p className="enter-values__hint">
            What the drawing shows. A check whose layout nobody states cannot choose which version of
            itself applies, and reports that instead of a verdict.
          </p>
          {needed.discriminators.map((discriminator) => (
            <div className="value-field" key={discriminator.name}>
              <label className="value-label" htmlFor={`d-${discriminator.name}`}>
                {discriminator.name}
                <span className="value-feeds">{discriminator.rule_ids.join(', ')}</span>
              </label>
              <select
                className="value-input value-input--wide"
                id={`d-${discriminator.name}`}
                value={choices[discriminator.name] ?? ''}
                onChange={(e) =>
                  setChoices((prior) => ({ ...prior, [discriminator.name]: e.target.value }))
                }
              >
                <option value="">Not stated</option>
                {discriminator.choices.map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </section>
      )}

      {error && (
        <div className="enter-values__error" role="alert">
          {error}
        </div>
      )}

      {stored.length > 0 && (
        <section className="enter-values__section" aria-live="polite">
          <h2>Stored, as the system read them</h2>
          <ul className="enter-values__stored">
            {stored.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      )}

      <footer className="enter-values__actions">
        <button type="button" className="value-primary" onClick={onSave} disabled={busy}>
          Save values
        </button>
        <button type="button" className="value-primary" onClick={onRunChecks} disabled={busy}>
          <Play size={14} aria-hidden="true" /> Run checks
        </button>
        {packageId && onDone && (
          <button type="button" className="value-secondary" onClick={() => onDone(packageId)}>
            See findings
          </button>
        )}
      </footer>

      {accepted && (
        <p className="enter-values__note" role="status">
          Checks queued ({accepted.slice(0, 8)}). The values shown above were saved first. A review
          worker now runs the deterministic checks; use <strong>See findings</strong> when it has
          completed.
        </p>
      )}
    </div>
  );
}

/**
 * The unconfirmed readings the AI took off the drawing this field comes from, as one-click chips.
 *
 * **Filtered by source, not guessed at.** A reading off the shop drawing is offered only for a
 * field the rulebook wants from the shop drawing. That is not a model deciding what a number means
 * — it is which sheet the number was physically read from, which is recorded, not inferred.
 *
 * Every reading for that sheet is offered, in the order the page holds them, with no ranking. A
 * "most likely" ordering would be exactly the guess this product does not make; the reviewer is
 * looking at the crop and the drawing, and they are the one who knows.
 *
 * Renders nothing when there is nothing to offer, so a field the reader found no candidates for is
 * an ordinary empty box rather than an empty promise.
 */
function AiReadings({
  quantity,
  candidates,
  busyId,
  onUse,
}: {
  quantity: Quantity;
  candidates: CandidateOut[];
  busyId: string | null;
  onUse: (candidate: CandidateOut) => void;
}) {
  const offered = candidates.filter((candidate) => candidate.source === quantity.source);
  const [open, setOpen] = useState(false);
  if (offered.length === 0) return null;

  const sheet = SOURCE_LABEL[quantity.source] ?? quantity.source;

  return (
    <div className="ai-readings">
      {/* **Collapsed, because the same readings are offered under every field of their sheet.**
          Filtering by source is all that can honestly be done today: which sheet a number was read
          from is recorded, and which field it belongs to is not — that is what the witness-line
          geometry in `extraction/geometry/dimension_lines.py` (#179) is being built to establish.

          With two readings and ten shop-drawing fields, showing them open put the same two values
          on screen twenty times and buried the fields themselves. Collapsed, the offer is still at
          the field that wants one, and the page still reads as a form. When geometry can say which
          field each reading measures, this stops being a list and becomes one suggestion. */}
      <button
        type="button"
        className="ai-readings__label"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        <ChevronRight size={12} className="collapsible-chevron" data-open={open} aria-hidden="true" />
        <ScanLine size={12} aria-hidden="true" />
        {offered.length} reading{offered.length === 1 ? '' : 's'} from the {sheet}
      </button>
      <div className="collapsible" data-open={open} inert={!open}>
      <div className="ai-readings__chips">
        {offered.map((candidate) => (
          <button
            key={candidate.candidate_id}
            type="button"
            className="ai-reading interactive"
            disabled={busyId !== null}
            onClick={() => onUse(candidate)}
            title={`Confirm ${candidate.value} as ${fieldLabel(quantity)}`}
          >
            <strong>{candidate.value}</strong>
            <span>p{candidate.page_index + 1}</span>
            {busyId === candidate.candidate_id ? <span>saving…</span> : <span>use</span>}
          </button>
        ))}
      </div>
      </div>
    </div>
  );
}

function MeasureCandidateCrop({ candidate, packageId }: { candidate: CandidateOut; packageId: string }) {
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
      () => {
        if (live) setState({ error: 'The stored crop could not be loaded.' });
      },
    );
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [candidate.candidate_id, packageId]);

  if (state && 'error' in state) {
    return <span className="ai-proposal__no-crop">{state.error}</span>;
  }
  if (!state || !('url' in state)) {
    return <span className="ai-proposal__crop-loading"><ScanLine size={14} aria-hidden="true" /> Loading crop…</span>;
  }
  return (
    <img
      className="ai-proposal__crop"
      src={state.url}
      alt={`Mechanical crop for ${candidate.raw_text} on page ${candidate.page_index + 1}`}
    />
  );
}
