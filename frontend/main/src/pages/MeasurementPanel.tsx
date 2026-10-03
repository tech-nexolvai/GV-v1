import { useEffect, useRef, useState } from 'react';
import type { SetStateAction } from 'react';
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
  calculateFillerDistribution,
  confirmCandidate,
  downloadCandidateCrop,
  downloadLayoutProposalCrop,
  downloadSettingPassageCrop,
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
import { DrawingRoles } from '../components/measure/DrawingRoles';
import { DrawingParts } from '../components/measure/DrawingParts';
import { FillerDistributionPanel } from '../components/measure/FillerDistributionPanel';
import { distributionFieldWidthKey } from '../components/measure/fillerDistribution';
import {
  categoryLabel,
  classificationEntries,
  isCategorical,
} from './classificationFields';
import { layoutChoiceDefaults } from './layoutChoices';
import { MeasurementGuidance, StoredProposalGuidance } from './MeasurementGuidance';
import { MeasurementSectionNav } from './MeasurementSectionNav';
import { jumpToMeasurementSection, measurementSections } from './measurementNavigation';
import { measurementValueOrigin, prefillReadingValues } from './measurementDraft';
import { loadMeasurementResources } from './measurementResources';
import { ReadingAvailability } from './ReadingAvailability';
import { appendConfirmedRunValue, confirmCandidateOnce, confirmProposalFields, newConfirmationLedger } from './measurementConfirmation';
import { settingMissingASource, type SettingSource } from '../components/measure/settingSources';
import { SettingCitation } from '../components/measure/SettingCitation';
import { SettingPassage, type PassageImage } from '../components/measure/SettingPassage';
import { loadPassageImage } from '../components/measure/passageImage';
import { ReadingConfirmationFeedback, type ReadingConfirmationState } from '../components/measure/ReadingConfirmationFeedback';
import {
  citingPointer,
  settingEntry,
  type SettingPointer,
} from '../components/measure/settingPointers';
import './MeasurementPanel.css';

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
  /** The choices, when this input is a category rather than a dimension — empty otherwise.
   *
   * A text box here would ask a reviewer to type `single_door` with a unit and the server would
   * refuse it. Non-empty means offer these, and send them back under `classifications`. */
  categories?: string[];
};
type Parameter = {
  name: string;
  scope: string;
  rule_ids: string[];
  declared_default: string | null;
  blocked: boolean;
  /** Where a value may come from (#827), in the order Raj's checklist gives them. */
  sources?: SettingSource[];
  /** Where the architect's drawing states it (#866): a page and a crop, never the number. */
  found?: SettingPointer | null;
};
type LayoutProposal = {
  value: string;
  crop_artifact_id: string;
  model_id: string;
  prompt_id: string;
  confirmed: boolean;
};
type Discriminator = {
  name: string;
  rule_ids: string[];
  choices: string[];
  proposal?: LayoutProposal | null;
};
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
/** One field a model proposed values for, already checked, as `required-inputs` sends it. */
type ProposedField = {
  field_key: string;
  name: string;
  source: string;
  many: boolean;
  /** Whether the drawing's own geometry confirmed where these readings sit. */
  placement_verified: boolean;
  values: { candidate_id: string; value: string; page_index: number }[];
};
type Needed = {
  quantities: Quantity[];
  confirmed_readings: ConfirmedReading[];
  proposed_readings: ProposedField[];
  parameters: Parameter[];
  discriminators: Discriminator[];
  rules_published: number;
  revision_state: string;
  still_reading: boolean;
};

type MeasurementDraft = {
  singles: Record<string, string>;
  runs: Record<string, string[]>;
  aiFilled: Record<string, string[]>;
};

const REQUIRED_INPUTS_POLL_MS = 2000;

/** Which sheet a measurement is read from, in the words a reviewer uses. */
const SOURCE_LABEL: Record<string, string> = {
  SHOP: 'shop drawing',
  ARCH: 'architectural drawing',
  USER_INPUT: 'measured on site',
  PRODUCT_SPEC: 'product specification',
};


/**
 * The drawings are being read, and this says so for as long as it takes.
 *
 * **The bar is indeterminate on purpose.** Reading a pair takes roughly a minute, but how long
 * *this* pair will take is not something this side of the request knows — it depends on the sheet,
 * the routes it needs and whether OCR runs. A bar that filled steadily would be asserting a
 * completion fraction nobody measured, which is the one thing the progress display on this product
 * is careful never to do. A segment crossing the track says "working" and claims nothing.
 *
 * The clock is measured here, and the stage name is the pipeline's own — both are facts. So a
 * reviewer waiting knows it is alive, roughly how long it has been, and which part of the work it
 * is in, without being told a number that was invented.
 */
function ReadingProgress({ state }: { state: string }) {
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef(0);

  useEffect(() => {
    // Started in the effect, not during render: React may render more than once per commit, so a
    // timestamp taken there can come from an attempt that was discarded.
    startedAt.current = Date.now();
    const timer = window.setInterval(() => setElapsed(Date.now() - startedAt.current), 200);
    return () => window.clearInterval(timer);
  }, []);

  const stage = state.toLowerCase().replace(/_/g, ' ');
  return (
    <div className="reading" role="status" aria-live="polite" aria-busy="true">
      <div className="reading__head">
        <ScanLine size={14} aria-hidden="true" />
        <span className="reading__title">Reading these drawings</span>
        <span className="reading__stage">{stage}</span>
        <span className="reading__clock mono">{(elapsed / 1000).toFixed(0)}s</span>
      </div>
      <div className="reading__track" aria-hidden="true">
        <span className="reading__bar" />
      </div>
      <p className="reading__note">
        Waiting for the reader. Available readings update here automatically; larger drawing sets
        can take longer. The timer shows time on this screen, not a completion estimate.
      </p>
    </div>
  );
}

/**
 * A semantic type as a person would say it, with the code kept beside it.
 *
 * `CT008` is what the rulebook matches on and what a reviewer quotes when asking about a field —
 * worth showing, useless on its own. Every quantity the rulebook asks for carries a readable name
 * through `consumers[].input_name`, and `fieldLabel` already renders it on the form; this reuses it
 * so one quantity cannot be called two different things on one screen.
 *
 * Falls back to the bare code where the rulebook asks for a quantity under no readable name, which
 * is honest: inventing a friendly word for a code nobody has defined would be worse than showing
 * the code.
 */
function quantityLabel(quantities: Quantity[], semanticType: string): string {
  const quantity = quantities.find((item) => item.semantic_type === semanticType);
  if (!quantity) return semanticType;
  const named = fieldLabel(quantity);
  return named === semanticType ? semanticType : `${named} (${semanticType})`;
}

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
  /**
   * **The weakest claim on the screen, and it says so.**
   *
   * On a scanned drawing there is no vector line-work, so nothing could confirm that this number
   * sits on the dimension it is supposed to measure. Every other check still passed — the right
   * sheet, one reading per field, a reading this run produced — but the geometry could not vouch
   * for where it is, and a reviewer has to know that before they keep it.
   */
  unplaced: 'proposed by AI — placement unchecked',
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

export function MeasurementPanel({
  packageId: selectedPackageId,
  onDone,
  onChoosePackage,
  onChecksRequested,
}: {
  packageId?: string;
  onDone?: (packageId: string) => void;
  onChoosePackage?: () => void;
  onChecksRequested?: () => void;
}) {
  const [needed, setNeeded] = useState<Needed | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [packageId, setPackageId] = useState<string | null>(null);
  // Values and proposal IDs update together, so a refreshed proposal never loses its provenance.
  const [{ singles, runs, aiFilled }, setDraft] = useState<MeasurementDraft>({ singles: {}, runs: {}, aiFilled: {} });
  function setSingles(value: SetStateAction<Record<string, string>>) {
    setDraft(prior => ({ ...prior, singles: typeof value === 'function' ? value(prior.singles) : value }));
  }
  function setRuns(value: SetStateAction<Record<string, string[]>>) {
    setDraft(prior => ({ ...prior, runs: typeof value === 'function' ? value(prior.runs) : value }));
  }
  function setAiFilled(value: SetStateAction<Record<string, string[]>>) {
    setDraft(prior => ({ ...prior, aiFilled: typeof value === 'function' ? value(prior.aiFilled) : value }));
  }
  const [reviewerEditedSingles, setReviewerEditedSingles] = useState<Set<string>>(() => new Set());
  const [choices, setChoices] = useState<Record<string, string>>({});
  /** Per setting: the source the reviewer chose, and where in it (#827). */
  const [sourceChoices, setSourceChoices] = useState<Record<string, string>>({});
  const [references, setReferences] = useState<Record<string, string>>({});
  /** Settings the app found a passage for, which the reviewer chose to enter another way (#866). */
  const [declinedCitations, setDeclinedCitations] = useState<Record<string, boolean>>({});
  const [stored, setStored] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidateOut[]>([]);
  const [semanticTypes, setSemanticTypes] = useState<string[]>([]);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [readingsError, setReadingsError] = useState<string | null>(null);
  const [vocabularyError, setVocabularyError] = useState<string | null>(null);
  const [loadingReadings, setLoadingReadings] = useState(false);
  const [confirmationFeedback, setConfirmationFeedback] = useState<Record<string, ReadingConfirmationState>>({});
  /** The phases of an assignment in flight, in arrival order. Empty until one is asked for. */
  const [proposalSteps, setProposalSteps] = useState<AssignmentStep[]>([]);
  const [proposal, setProposal] = useState<ProposedMeasurements | null>(null);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  /** Whether the crop-inspection list is open. Closed by default; it is the slow route. */
  const [inspecting, setInspecting] = useState(false);
  /** Bumped to re-fetch the form while the reader is still working. */
  const [reload, setReload] = useState(0);
  /**
   * Which fields a model proposed, by field key, with the readings it chose.
   *
   * **Nothing a model proposed reaches a reviewer unmarked.** A value that appeared in a box with
   * no explanation is indistinguishable from one a person read off the drawing, and the reviewer's
   * confirmation is the only thing standing between a proposal and a verdict. The mark is dropped
   * the moment they type in the field: it is theirs from then on.
   */
  const reviewerEditedSinglesRef = useRef<Set<string>>(new Set());
  const loadedPackageRef = useRef<string | undefined>(undefined);
  const confirmationLedger = useRef(newConfirmationLedger());
  const appliedConfirmations = useRef(new Set<string>());
  const formContainer = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    let poll: ReturnType<typeof window.setTimeout> | undefined;
    // A package switch must never leave the prior package's fields enabled while the new contract is
    // loading. The reviewer could otherwise submit a value against the wrong drawing pair.
    if (loadedPackageRef.current !== selectedPackageId) {
      loadedPackageRef.current = selectedPackageId;
      confirmationLedger.current = newConfirmationLedger();
      appliedConfirmations.current = new Set();
      setPackageId('');
      setNeeded(null);
      setDraft({ singles: {}, runs: {}, aiFilled: {} });
      const freshEdits = new Set<string>();
      reviewerEditedSinglesRef.current = freshEdits;
      setReviewerEditedSingles(freshEdits);
      setChoices({});
      setSourceChoices({});
      setReferences({});
      setDeclinedCitations({});
      setCandidates([]);
      setSemanticTypes([]);
      setCandidateError(null);
      setReadingsError(null);
      setVocabularyError(null);
      setConfirmationFeedback({});
      setConfirming(null);
      setLoadError(null);
      setProposalSteps([]);
      setProposal(null);
      setProposalError(null);
    }
    const applyRequiredInputs = (required: Needed) => {
      const confirmedByKey = required.confirmed_readings.reduce<Record<string, string[]>>(
        (grouped, reading) => ({
          ...grouped,
          [reading.key]: [...(grouped[reading.key] ?? []), reading.value],
        }),
        {},
      );
      const edited = new Set(reviewerEditedSinglesRef.current);
      setDraft((prior) => {
        const next = { singles: { ...prior.singles }, runs: { ...prior.runs }, aiFilled: { ...prior.aiFilled } };
        for (const quantity of required.quantities) {
          const current = quantity.many ? (next.runs[quantity.key] ?? ['']) : [next.singles[quantity.key] ?? ''];
          const confirmed = confirmedByKey[quantity.key] ?? [];
          const proposal = required.proposed_readings?.find((field) => field.field_key === quantity.key);
          const merged = prefillReadingValues(
            current, edited.has(quantity.key), confirmed,
            proposal?.values.map((reading) => reading.value) ?? [], quantity.many,
          );
          if (quantity.many) next.runs[quantity.key] = merged.values;
          else next.singles[quantity.key] = merged.values[0] ?? '';
          if (merged.origin === 'proposed' && proposal) {
            next.aiFilled[quantity.key] = proposal.values.map((reading) => reading.candidate_id);
          } else if (merged.origin === 'confirmed') {
            delete next.aiFilled[quantity.key];
          }
        }
        return next;
      });
      setChoices((prior) => {
        const defaults = layoutChoiceDefaults(required.discriminators);
        if (Object.keys(defaults).length === 0) return prior;
        return { ...defaults, ...prior };
      });
    };

    const load = async (includeVocabulary: boolean) => {
      if (!selectedPackageId) return;
      setLoadingReadings(true);
      try {
        const { fields, readings, vocabulary } = await loadMeasurementResources(
          getRequiredInputs(projectId(), selectedPackageId),
          listCandidates(projectId(), selectedPackageId),
          includeVocabulary ? listSemanticTypes() : Promise.resolve<string[]>([]),
        );
        if (cancelled) return;
        const required = fields as unknown as Needed;
        setPackageId(selectedPackageId);
        setNeeded(required);
        setLoadError(null);
        setCandidates(readings.value?.candidates ?? []);
        setReadingsError(readings.error);
        if (includeVocabulary) {
          setSemanticTypes(vocabulary.value ?? []);
          setVocabularyError(vocabulary.error);
        }
        applyRequiredInputs(required);
        if (required.still_reading) {
          poll = window.setTimeout(() => void load(false), REQUIRED_INPUTS_POLL_MS);
        }
      } catch (caught) {
        if (!cancelled) {
          setLoadError(caught instanceof ApiError ? caught.message : String(caught));
        }
      } finally {
        if (!cancelled) setLoadingReadings(false);
      }
    };
    void load(true);
    return () => {
      cancelled = true;
      if (poll !== undefined) window.clearTimeout(poll);
    };
    // Re-fetch only when the selected package changes, never on a keystroke within its form.
  }, [selectedPackageId, reload]);

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

  async function confirmFromMeasure(candidate: CandidateOut, semanticType: string, fromField = false) {
    if (!packageId || !needed || !candidate.source || !candidate.value) return;
    if (appliedConfirmations.current.has(candidate.candidate_id)) return;
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
    setConfirmationFeedback((prior) => ({ ...prior, [target.key]: { kind: 'saving' } }));
    const requestLedger = confirmationLedger.current;
    try {
      const result = await confirmCandidateOnce(
        requestLedger, candidate.candidate_id, semanticType,
        (id, type) => confirmCandidate(projectId(), packageId, id, type),
      );
      if (confirmationLedger.current !== requestLedger) return;
      // Concurrent clicks share one request and apply its persisted reading only once.
      if (appliedConfirmations.current.has(candidate.candidate_id)) return;
      appliedConfirmations.current.add(candidate.candidate_id);
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
          return {
            ...prior,
            [target.key]: appendConfirmedRunValue(prior[target.key] ?? [], reading.value,
              candidate.candidate_id, aiFilled[target.key]),
          };
        });
      } else {
        // A reviewer-entered value has priority in the editable form; do not erase it behind their
        // back merely because they later confirm an AI proposal.
        setSingles((prior) => {
          const readingCount =
            needed.confirmed_readings.filter((item) => item.key === key).length + 1;
          if (reviewerEditedSinglesRef.current.has(target.key)) return prior;
          return { ...prior, [target.key]: readingCount === 1 ? reading.value : '' };
        });
      }
      setCandidates((current) =>
        current.filter((item) => item.candidate_id !== candidate.candidate_id),
      );
      setConfirmationFeedback((prior) => ({ ...prior, [target.key]: {
        kind: 'confirmed',
        preserved: !target.many && reviewerEditedSinglesRef.current.has(target.key),
        multiple: !target.many && needed.confirmed_readings.some((item) => item.key === key),
      } }));
    } catch (caught) {
      if (confirmationLedger.current !== requestLedger) return;
      const message = caught instanceof Error ? caught.message : 'This AI reading could not be confirmed.';
      setConfirmationFeedback((prior) => ({ ...prior, [target.key]: { kind: 'error', message } }));
      if (!fromField) setCandidateError(message);
    } finally {
      if (confirmationLedger.current === requestLedger) setConfirming(null);
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
  /**
   * Accepting an AI reading is confirming a drawing reading, not typing a number.
   *
   * **This is what kept a crop off the screen.** A proposal knows which reading it came from, and
   * that reading has a stored crop cut from the uploaded PDF. But saving put the *number* into the
   * parameter set as a reviewer-supplied value — and `run_checks` deliberately lets a supplied
   * operand override a derived one, because "supplying one is a deliberate act". So the check was
   * judged on `USER_INPUT: 18 in`, with no page, no polygon and no crop, while the drawing reading
   * that produced it sat unused beside it. Asked for the evidence, the panel could only say there
   * was none.
   *
   * So an unedited proposal is confirmed instead: the candidate is sealed as a canonical
   * observation the reviewer stands behind, which carries its page, its polygon and its crop, and
   * the check reads it through the evidence path. The reviewer's act is the same one click; what
   * changes is that the record keeps hold of where the number came from.
   *
   * Returns the field keys it confirmed, so the caller can leave them out of the typed payload —
   * sending both would put a value with no provenance in front of the one with it.
   */
  async function confirmAcceptedProposals(): Promise<Set<string>> {
    if (!packageId || !needed) return new Set();
    const fields = Object.entries(aiFilled).flatMap(([key, candidateIds]) => {
      const quantity = needed.quantities.find((item) => item.key === key);
      if (!quantity || candidateIds.length === 0) return [];
      return [{ key, candidateIds, semanticType: quantity.semantic_type }];
    });
    // A partial failure stops the save. Successful receipts survive retries, because the backend
    // rejects duplicate confirmations; a 409 or lost response is never guessed to be success.
    return confirmProposalFields(fields, confirmationLedger.current,
      (id, type) => confirmCandidate(projectId(), packageId, id, type));
  }

  async function saveVisibleValues(): Promise<boolean> {
    if (!packageId || !needed) return false;
    // A setting typed from the passage the app found takes its source from that passage (#866).
    const unsourced = settingMissingASource(
      needed.parameters.filter((p) => !p.blocked && !citingPointer(p, declinedCitations)),
      singles,
      sourceChoices,
    );
    if (unsourced) {
      setError(`Say where ${unsourced} came from before saving.`);
      return false;
    }
    try {
      const confirmedFromDrawing = await confirmAcceptedProposals();

      // **One typed value fans out to every rule input it feeds.** The mapping is the server's, taken
      // from `consumers` — a reviewer measures the front offset once, and three rules receive it.
      const measurements = needed.quantities.flatMap<MeasurementEntry>((quantity) => {
        // Confirmed above, so it reaches the checks as drawing-backed evidence. Typing it as well
        // would shadow that with a value carrying no page, no polygon and no crop.
        if (confirmedFromDrawing.has(quantity.key)) return [];
        // A category is not a dimension and travels under `classifications` below (#684). Sending
        // it here would put `single_door` through the unit parser, which refuses it.
        if (isCategorical(quantity)) return [];
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

      const classifications = classificationEntries(needed.quantities, runs);

      // A setting typed from a found passage sends that passage as its citation, and the server
      // holds the number to it (#866); any other setting goes exactly as it always has.
      const parameters = needed.parameters
        .filter((p) => !p.blocked && (singles[p.name] ?? '').trim())
        .map((p) =>
          settingEntry(p, singles[p.name] ?? '', {
            declined: declinedCitations,
            source: sourceChoices[p.name],
            reference: references[p.name],
          }),
        );

      const result = await enterMeasurements(projectId(), packageId, {
        parameters,
        measurements,
        classifications,
      });
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
      setAiFilled((prior) => ({ ...prior, ...marks }));
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
    const edited = new Set(reviewerEditedSinglesRef.current).add(key);
    reviewerEditedSinglesRef.current = edited;
    setReviewerEditedSingles(edited);
    // A receipt describes the previous insertion, not the value being edited now. Keep pending
    // and failed requests visible; removing this UI receipt does not undo the stored confirmation.
    setConfirmationFeedback((prior) => {
      if (prior[key]?.kind !== 'confirmed') return prior;
      const next = { ...prior };
      delete next[key];
      return next;
    });
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
      onChecksRequested?.();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (loadError && !needed) {
    return (
      <div className="enter-values">
        <div className="enter-values__error" role="alert">
          {loadError}
          <button type="button" className="value-secondary" onClick={() => setReload((count) => count + 1)}>Try again</button>
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
  const fieldOrigin = (quantity: Quantity) => measurementValueOrigin({
    hasValue: hasValue(quantity),
    humanEdited: reviewerEditedSingles.has(quantity.key),
    proposed: quantity.key in aiFilled,
    placementUnverified: unverifiedFields.has(quantity.key),
    qualifications: (readingsByKey[quantity.key] ?? []).map((reading) => reading.qualification),
  });

  /** How many fields the filed proposal covers. Zero when nothing was filed, which is a real
   *  outcome: the checks refused the model's answer, or the reader attached nothing to fill from. */
  const storedProposalCount = (needed.proposed_readings ?? []).length;

  /** Fields whose placement nothing could confirm, because the drawing carries no line-work. */
  const unverifiedFields = new Set(
    (needed.proposed_readings ?? [])
      .filter((field) => !field.placement_verified)
      .map((field) => field.field_key),
  );

  /** Whether a proposal has been asked for at all. What decides who owns the panel's space. */
  const attempted = proposing || proposalSteps.length > 0 || proposal !== null || proposalError !== null;

  /** The sheets this rulebook reads from, in the order the fields appear. One group per sheet. */
  const sheets = needed.quantities.reduce<string[]>(
    (seen, quantity) => (seen.includes(quantity.source) ? seen : [...seen, quantity.source]),
    [],
  );
  const fieldDimensionKey = distributionFieldWidthKey(needed.quantities);
  const sections = measurementSections(
    sheets.map((sheet) => ({ key: sheet, label: SOURCE_LABEL[sheet] ?? sheet })),
    needed.discriminators.length > 0,
    stored.length > 0,
  );

  return (
    <div className="enter-values" ref={formContainer}>
      <MeasurementSectionNav sections={sections} onJump={(key) => jumpToMeasurementSection(formContainer.current, key)} />
      {loadError && (
        <div className="enter-values__error" role="alert">
          New readings could not be loaded. Your entries are retained. {loadError}
          <button type="button" className="value-secondary" onClick={() => setReload((count) => count + 1)}>Retry</button>
        </div>
      )}
      <MeasurementGuidance rulesPublished={needed.rules_published} />
      {readingsError && <ReadingAvailability error={readingsError} retrying={loadingReadings}
        onRetry={() => setReload((count) => count + 1)} />}
      {vocabularyError && <div className="enter-values__error" role="alert">
        Reading meanings could not be loaded. {vocabularyError}
        <button type="button" className="value-secondary" disabled={loadingReadings}
          onClick={() => setReload((count) => count + 1)}>Retry reading meanings</button>
      </div>}

      {/* **Before any reading can fill a field on a combined sheet** (#795): a reading is used only on
          the side of the drawing it sits in, so the drawings' roles come first. Nothing renders for
          a package of two separate PDFs. Each answer re-reads the readings, which now have a side. */}
      {packageId && (
        <DrawingRoles
          key={`roles:${packageId}`}
          packageId={packageId}
          onConfirmed={() => setReload((count) => count + 1)}
        />
      )}

      {/* **The parts of each vendor drawing** (#882): suggested, and each one decided by a person. */}
      {packageId && <DrawingParts key={`parts:${packageId}`} packageId={packageId} refresh={reload} />}

      <section className="enter-values__section">
        <h2 data-measure-section="overview" tabIndex={-1}>Measurements</h2>

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
            aria-label="Measurement fields with a value, not reading accuracy"
            aria-valuenow={coveragePercent}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <span className="measure-coverage__bar" style={{ width: `${coveragePercent}%` }} />
          </div>
          <ul className="measure-coverage__counts">
            {readingsError ? <li>Drawing reading total unavailable</li> : needed.still_reading && candidates.length + confirmedCount === 0 ? (
              <li>
                <strong>Reading</strong> dimensions from the drawings
              </li>
            ) : (
              <li>
                <strong>{candidates.length + confirmedCount}</strong> dimensions read off the drawings
              </li>
            )}
            <li>
              <strong>{confirmedCount}</strong> confirmed by a reviewer
            </li>
            <li>
              {readingsError ? 'Unconfirmed reading count unavailable' : <><strong>{candidates.length}</strong> not yet given a meaning</>}
            </li>
            {exactTagFieldCount > 0 && (
              <li>
                <strong>{exactTagFieldCount}</strong> filled with no click — the drawing states the
                meaning itself
              </li>
            )}
          </ul>
          {needed.still_reading && <ReadingProgress state={needed.revision_state} />}
          <p className="measure-coverage__caveat">
            Form coverage, not accuracy. A filled field still needs review; an empty field needs your input.
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
        {needed.still_reading ? (
          <div className="measure-fill measure-fill--reading">
            <div className="measure-fill__text">
              <h3>
                <ScanLine size={15} aria-hidden="true" /> Reading drawings
              </h3>
              <p>
                Current stage: <code>{needed.revision_state}</code>. This page will check again
                while the reader is working.
              </p>
            </div>
          </div>
        ) : !attempted && storedProposalCount > 0 ? (
          /* **Already done, before the reviewer arrived.** The proposal is made when the drawings
             are read and filed, so this panel reports a completed step rather than offering one.
             The offer below is what a package with no filed proposal still shows. */
          <div className="measure-fill measure-fill--done">
            <div className="measure-fill__text">
              <h3>
                <Sparkles size={15} aria-hidden="true" /> AI suggestions ready to review
              </h3>
              <StoredProposalGuidance count={storedProposalCount} unverifiedCount={unverifiedFields.size} />
            </div>
            <button
              type="button"
              className="value-secondary interactive"
              onClick={() => void onPropose()}
              disabled={busy || candidates.length === 0}
            >
              <Sparkles size={13} aria-hidden="true" /> Ask again
            </button>
          </div>
        ) : !attempted ? (
          <div className="measure-fill">
            <div className="measure-fill__text">
              <h3>Suggest values from drawing readings</h3>
              <p>
                Ask AI to suggest where the available readings belong. Review suggestions against
                their crops before saving, or enter values yourself. This does not run the checks.
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
            {candidates.length === 0 && !readingsError && (
              <p className="enter-values__hint enter-values__hint--tight">
                {needed.still_reading
                  ? `The AI is still reading these drawings (${needed.revision_state.toLowerCase().replace(/_/g, ' ')}). This page is watching, and will fill itself in when the reading finishes.`
                  : confirmedCount > 0
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
                    <span>
                      {SOURCE_LABEL[candidate.source ?? ''] ??
                        candidate.source_refusal ??
                        'drawing source unavailable'}
                    </span>
                    <span>p{candidate.page_index + 1}</span>
                    {/* No confidence score — it is written only by RapidOCR, which produced 903
                        candidates and zero values on the real drawing. #720. */}
                  </div>
                  {candidate.crop_key && packageId ? (
                    <MeasureCandidateCrop candidate={candidate} packageId={packageId} />
                  ) : (
                    <p className="ai-proposal__no-crop">
                      No crop is available, so this reading cannot be confirmed here.
                    </p>
                  )}
                  <label className="ai-proposal__type">
                    {/* **The codes are not a vocabulary anybody has.**
                        This listed `CT004`, `CT008`, `cabinet_width` — the rulebook's internal
                        keys — and asked a reviewer to pick one. Nobody outside the rule engine
                        knows what `CT008` measures, so the only honest thing a person could do was
                        guess or give up. The rulebook already names every one of them readably and
                        the page already uses those names on its own fields; this is the same
                        `fieldLabel`, so the dropdown and the form cannot call one quantity two
                        things. */}
                    <span>What does this number measure?</span>
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
                      <option value="">Choose what it measures…</option>
                      {availableTypes.map((semanticType) => (
                        <option key={semanticType} value={semanticType}>
                          {quantityLabel(needed.quantities, semanticType)}
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
            {candidateError && <p className="enter-values__error" role="alert">{candidateError}</p>}
          </section>
        )}
        {needed.confirmed_readings.length > 0 ? (
          <p className="enter-values__hint" role="status">
            {needed.confirmed_readings.length} qualified drawing reading{needed.confirmed_readings.length === 1 ? '' : 's'} on record.
            Review the current field values before saving; your edits do not change those recorded readings.
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
                <h3 className="sheet-group__title" data-measure-section={`source:${sheet}`} tabIndex={-1}>
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
                      {quantity.key in aiFilled && (
                        <span className="value-where">
                          {aiFilled[quantity.key].length} reading
                          {aiFilled[quantity.key].length === 1 ? '' : 's'} from the{' '}
                          {SOURCE_LABEL[quantity.source] ?? quantity.source}
                        </span>
                      )}
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
                      feedbackId={confirmationFeedback[quantity.key] ? `confirmation-${quantity.key}` : undefined}
                      onUse={(candidate) => void confirmFromMeasure(candidate, quantity.semantic_type, true)}
                    />
                    <ReadingConfirmationFeedback state={confirmationFeedback[quantity.key]} id={`confirmation-${quantity.key}`} />
                    {quantity.many ? (
                      <>
                        {(runs[quantity.key] ?? ['']).map((value, index) => (
                          <div className="value-row" key={index}>
                            {isCategorical(quantity) ? (
                              <select
                                className="value-input value-input--wide"
                                id={index === 0 ? `q-${quantity.key}` : undefined}
                                aria-label={`${fieldLabel(quantity)}, item ${index + 1}, left to right`}
                                value={value}
                                onChange={(e) => {
                                  releaseField(quantity.key);
                                  setRun(quantity.key, index, e.target.value);
                                }}
                              >
                                <option value="">Not classified</option>
                                {(quantity.categories ?? []).map((category) => (
                                  <option key={category} value={category}>
                                    {categoryLabel(category)}
                                  </option>
                                ))}
                              </select>
                            ) : (
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
                            )}
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
                          const nextEdits = new Set(reviewerEditedSinglesRef.current).add(
                            quantity.key,
                          );
                          reviewerEditedSinglesRef.current = nextEdits;
                          setReviewerEditedSingles(nextEdits);
                          setSingles((prior) => ({ ...prior, [quantity.key]: e.target.value }));
                        }}
                      />
                    )}
                    {/* **An empty field used to say nothing at all.**
                        A reviewer new to the product sees fourteen boxes and no indication of
                        where any number comes from. The rulebook knows: which sheet it is read
                        from, and what the client's own code book says the quantity is. Both were
                        already loaded and neither was on the screen. */}
                    {origin === 'empty' && (
                      <p className="value-help">
                        Enter the value from the <strong>{SOURCE_LABEL[quantity.source] ?? quantity.source}</strong>
                        {' '}with its unit, or inspect an available reading before using it.
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
      </section>

      <section className="enter-values__section">
        <h2 data-measure-section="settings" tabIndex={-1}>Settings</h2>
        <p className="enter-values__hint">
          Values for this job rather than dimensions off a drawing. Where the rulebook suggests one it
          is shown — a rule author&apos;s stand-in, not a number the client has confirmed. Where the
          architect&apos;s drawing states one, the page is shown but the number is not: type what you
          see, and it is saved only if it matches.
        </p>
        {needed.parameters.map((parameter) => {
          const pointer = citingPointer(parameter, declinedCitations);
          return (
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
              ) : pointer ? (
                /* **Blind entry (#866).** The box shows where the architect's drawing states this
                   setting and starts empty; the app's own reading of the number never reaches the
                   page. Save sends the passage as the citation, and a number that differs from the
                   drawing's is refused with the server's sentence below. */
                <SettingCitation
                  name={parameter.name}
                  pointer={pointer}
                  value={singles[parameter.name] ?? ''}
                  crop={
                    packageId ? (
                      <SettingPassageCrop
                        key={`${packageId}:${pointer.proposal_id}`}
                        packageId={packageId}
                        pointer={pointer}
                        name={parameter.name}
                      />
                    ) : null
                  }
                  onChange={(value) =>
                    setSingles((prior) => ({ ...prior, [parameter.name]: value }))
                  }
                  onDecline={() =>
                    setDeclinedCitations((prior) => ({ ...prior, [parameter.name]: true }))
                  }
                />
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
                  <SettingSourceFields
                    parameter={parameter}
                    chosen={sourceChoices[parameter.name] ?? ''}
                    reference={references[parameter.name] ?? ''}
                    onChoose={(value) =>
                      setSourceChoices((prior) => ({ ...prior, [parameter.name]: value }))
                    }
                    onReference={(value) =>
                      setReferences((prior) => ({ ...prior, [parameter.name]: value }))
                    }
                  />
                  {parameter.found && (
                    <button
                      type="button"
                      className="value-secondary setting-citation__decline"
                      onClick={() =>
                        setDeclinedCitations((prior) => ({ ...prior, [parameter.name]: false }))
                      }
                    >
                      Type it from the architect&apos;s drawing, page {parameter.found.page_index + 1}
                    </button>
                  )}
                </>
              )}
            </div>
          );
        })}
      </section>

      {/* The calculator follows the measurements and limits it depends on. Its request and
          reviewer-owned input handlers are unchanged; moving it never applies a proposal. */}
      <FillerDistributionPanel
        quantities={needed.quantities}
        parameters={needed.parameters}
        singles={singles}
        runs={runs}
        fieldWidth={fieldDimensionKey ? (singles[fieldDimensionKey] ?? '') : ''}
        onFieldWidthChange={(value) => {
          if (!fieldDimensionKey) return;
          const nextEdits = new Set(reviewerEditedSinglesRef.current).add(fieldDimensionKey);
          reviewerEditedSinglesRef.current = nextEdits;
          setReviewerEditedSingles(nextEdits);
          setSingles((prior) => ({ ...prior, [fieldDimensionKey]: value }));
        }}
        onCalculate={(request) => calculateFillerDistribution(projectId(), request)}
      />

      {needed.discriminators.length > 0 && (
        <section className="enter-values__section">
          <h2 data-measure-section="layout" tabIndex={-1}>Layout</h2>
          <p className="enter-values__hint">
            What the drawing shows. Proposed answers are pre-selected with the crop that supports
            them. A blank field means the reader abstained, so choose the value from the drawing or
            leave it unstated.
          </p>
          {Object.keys(layoutChoiceDefaults(needed.discriminators)).length > 0 && (
            <p className="layout-confirm-note" role="status">
              Run checks records the selected layout answers together. Change either select before
              running checks when the crop shows a different layout.
            </p>
          )}
          {needed.discriminators.map((discriminator) => (
            <div
              className="value-field layout-field"
              data-proposed={discriminator.proposal ? 'true' : 'false'}
              key={discriminator.name}
            >
              <div className="layout-field__control">
                <label className="value-label" htmlFor={`d-${discriminator.name}`}>
                  {discriminator.name}
                  {discriminator.proposal ? (
                    <span className="value-origin value-origin--proposed">proposed</span>
                  ) : (
                    <span className="value-origin value-origin--empty">needs you</span>
                  )}
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
              {discriminator.proposal ? (
                <LayoutProposalCrop
                  packageId={packageId ?? ''}
                  proposal={discriminator.proposal}
                  discriminatorName={discriminator.name}
                />
              ) : (
                <p className="layout-field__empty">
                  No proposed answer was recorded for this layout question.
                </p>
              )}
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
          <h2 data-measure-section="stored" tabIndex={-1}>Stored, as the system read them</h2>
          <ul className="enter-values__stored">
            {stored.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      )}

      <footer className="enter-values__actions" data-measure-section="actions" tabIndex={-1} aria-label="Save and run controls">
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
  feedbackId,
  onUse,
}: {
  quantity: Quantity;
  candidates: CandidateOut[];
  busyId: string | null;
  feedbackId?: string;
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
            aria-describedby={feedbackId}
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

function LayoutProposalCrop({
  packageId,
  proposal,
  discriminatorName,
}: {
  packageId: string;
  proposal: LayoutProposal;
  discriminatorName: string;
}) {
  const [state, setState] = useState<{ url: string } | { error: string } | null>(null);

  useEffect(() => {
    if (!packageId) return;
    let live = true;
    let objectUrl: string | null = null;
    void downloadLayoutProposalCrop(projectId(), packageId, proposal.crop_artifact_id).then(
      (blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (live) setState({ url: objectUrl });
        else URL.revokeObjectURL(objectUrl);
      },
      () => {
        if (live) setState({ error: 'The stored layout crop could not be loaded.' });
      },
    );
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [packageId, proposal.crop_artifact_id]);

  if (state && 'error' in state) {
    return <p className="layout-field__empty">{state.error}</p>;
  }
  if (!state || !('url' in state)) {
    return (
      <span className="ai-proposal__crop-loading">
        <ScanLine size={14} aria-hidden="true" /> Loading crop…
      </span>
    );
  }
  return (
    <figure className="layout-crop">
      <img
        className="ai-proposal__crop"
        src={state.url}
        alt={`Plan-view crop for ${discriminatorName}: ${proposal.value}`}
      />
      <figcaption>Read from the plan view</figcaption>
    </figure>
  );
}

/**
 * The crop of the passage that states a setting (#866): pixels only, for the reviewer to read the
 * number off. Nothing parsed from it is fetched or shown.
 */
function SettingPassageCrop({
  packageId,
  pointer,
  name,
}: {
  packageId: string;
  pointer: SettingPointer;
  name: string;
}) {
  const [image, setImage] = useState<PassageImage>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => loadPassageImage(
    () => downloadSettingPassageCrop(projectId(), packageId, pointer.proposal_id),
    (url) => setImage({ url }),
    () => setImage({ error: true }),
  ), [packageId, pointer.proposal_id, attempt]);

  return <SettingPassage pointer={pointer} name={name} image={image}
    onImageError={() => setImage({ error: true })}
    onRetry={() => { setImage(null); setAttempt((prior) => prior + 1); }} />;
}

/**
 * Where a setting came from (#827). One allowed source is stated, not asked; several are a choice.
 * The guidance line carries Q10 for the G.C / Client source: never copied from the vendor's drawing.
 */
function SettingSourceFields({
  parameter,
  chosen,
  reference,
  onChoose,
  onReference,
}: {
  parameter: Parameter;
  chosen: string;
  reference: string;
  onChoose: (value: string) => void;
  onReference: (value: string) => void;
}) {
  const sources = parameter.sources ?? [];
  if (sources.length === 0) return null;
  const current = sources.length === 1 ? sources[0].value : chosen;
  const selected = sources.find((source) => source.value === current);
  return (
    <div className="setting-source">
      {sources.length === 1 ? (
        <p className="enter-values__hint enter-values__hint--tight">
          Source: <strong>{sources[0].value}</strong>
        </p>
      ) : (
        <label className="setting-source__choice" htmlFor={`p-${parameter.name}-source`}>
          Where from?
          <select
            className="value-input"
            id={`p-${parameter.name}-source`}
            value={chosen}
            onChange={(e) => onChoose(e.target.value)}
          >
            <option value="">Choose…</option>
            {sources.map((source) => (
              <option key={source.value} value={source.value}>
                {source.value}
              </option>
            ))}
          </select>
        </label>
      )}
      {selected && <p className="enter-values__hint enter-values__hint--tight">{selected.guidance}</p>}
      <input
        className="value-input value-input--wide"
        id={`p-${parameter.name}-reference`}
        aria-label={`Where in the source ${parameter.name} is`}
        placeholder="Reference (optional), e.g. Architect A-501, section 3"
        maxLength={200}
        value={reference}
        onChange={(e) => onReference(e.target.value)}
      />
    </div>
  );
}
