import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
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
import { CountertopRuns } from '../components/measure/CountertopRuns';
import { SlotReaderRows } from '../components/measure/SlotReaderRows';
import { DrawingParts } from '../components/measure/DrawingParts';
import { PageDrawing } from '../components/measure/PageDrawing';
import { ReadingParts } from '../components/measure/ReadingParts';
import { FillerDistributionPanel } from '../components/measure/FillerDistributionPanel';
import { MeasurementSectionNav } from './MeasurementSectionNav';
// The redesigned components (#1124): the whole wizard is Tailwind + shadcn now, no legacy classes.
import { InfoTip } from '@/components/ui/info-tip';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';
import { Caution, Hint, INPUT_CLASS, LoadError, SELECT_CLASS, StepSection } from '../components/measure/wizard-ui';
import { countWords } from '../components/measure/wizardWords';
import { MEASURE_STEPS, sameCount, stepAfter, stepBefore, stepIsEmpty, sumCounts, type MeasureStep, type SectionState, type StepCount } from '@/lib/measure-steps';
import { measurementValueOrigin } from './measurementValueOrigin';
import { proposalCoversEveryPosition, proposalValuesAtPositions } from '../lib/measurement-proposals';
import { fieldReview, reviewCounts, type FieldReview, type ReviewedCandidate } from './fieldReview';
import { distributionFieldWidthKey } from '../components/measure/fillerDistribution';
import {
  categoryLabel,
  classificationEntries,
  isCategorical,
} from './classificationFields';
import { layoutChoiceDefaults } from './layoutChoices';
import { settingMissingASource, type SettingSource } from '../components/measure/settingSources';
import { SettingCitation } from '../components/measure/SettingCitation';
import { candidateCropWarning } from '../components/measure/candidateCropWarning';
import { packageChanged, refreshFailureIsFatal } from './measureRefreshState';
import {
  citingPointer,
  settingEntry,
  uploadLabel,
  type SettingPointer,
} from '../components/measure/settingPointers';

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
  /** Where a value may come from (#827), in the order the client lead's checklist gives them. */
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
  requires_confirmation: boolean;
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
  page_index: number;
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
  /** Expected positions in a many-valued row, including blank slots. */
  expected_count?: number | null;
  values: { candidate_id: string; value: string; page_index: number; position?: number | null }[];
};
type Needed = {
  page_numbers: number[];
  quantities: Quantity[];
  confirmed_readings: ConfirmedReading[];
  proposed_readings: ProposedField[];
  parameters: Parameter[];
  discriminators: Discriminator[];
  rules_published: number;
  revision_state: string;
  still_reading: boolean;
};

const REQUIRED_INPUTS_POLL_MS = 5000;
/** Run checks ignores clicks this soon after a step change: they were meant for the step's Next. */
const RUN_CHECKS_SETTLE_MS = 300;

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
    <div data-slot="reading-progress" className="flex flex-col gap-2 rounded-xl border bg-card p-4" role="status" aria-live="polite" aria-busy="true">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <ScanLine className="size-4" aria-hidden="true" />
        <span className="font-semibold">Reading these drawings</span>
        <span className="text-muted-foreground">{stage}</span>
        <span className="num ml-auto text-muted-foreground">{(elapsed / 1000).toFixed(0)}s</span>
      </div>
      {/* Indeterminate: a pulse says "working" and claims no fraction nobody measured. */}
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-primary/15" aria-hidden="true">
        <span className="block h-full w-1/3 animate-pulse rounded-full bg-primary motion-reduce:animate-none" />
      </div>
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        Usually about a minute; this page fills itself in.
        <InfoTip label="About the reading">
          <p>This usually takes about a minute. The page is watching and will fill itself in when the reading finishes — you do not need to reload.</p>
        </InfoTip>
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
  onChoosePackage,
  onChecksQueued,
  onValuesSaved,
  targetRow,
  onTargetReached,
  onReviewRow,
  onOpenQueue,
  runChecksRequest,
}: {
  packageId?: string;
  onChoosePackage?: () => void;
  onChecksQueued?: () => void;
  /** Values were saved without running checks (#1034: the stepper then asks for a new run). */
  onValuesSaved?: () => void;
  targetRow?: string | null;
  onTargetReached?: () => void;
  onReviewRow?: (rowId: string) => void;
  /** Opens the "Needs you" queue (#1050): where a held countertop row is decided. */
  onOpenQueue?: () => void;
  /**
   * Bumped by the review header's "Run checks" (#1124): Run checks lives only on the last step, so
   * each new request opens that step, where `#measure-run-checks` is.
   */
  runChecksRequest?: number;
}) {
  const [needed, setNeeded] = useState<Needed | null>(null);
  const [selectedPageNumber, setSelectedPageNumber] = useState(1);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [packageId, setPackageId] = useState<string | null>(null);
  /** Single-valued quantities and parameters, keyed by quantity key or parameter name. */
  const [singles, setSingles] = useState<Record<string, string>>({});
  const [reviewerEditedSingles, setReviewerEditedSingles] = useState<Set<string>>(() => new Set());
  const [reviewerEditedMany, setReviewerEditedMany] = useState<Set<string>>(() => new Set());
  /** Many-valued quantities, in layout order. */
  const [runs, setRuns] = useState<Record<string, string[]>>({});
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
  const [candidateLoadError, setCandidateLoadError] = useState<string | null>(null);
  const [vocabularyLoadError, setVocabularyLoadError] = useState<string | null>(null);
  const [resourceRetry, setResourceRetry] = useState(0);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [candidateChoices, setCandidateChoices] = useState<Record<string, string>>({});
  const [candidateErrors, setCandidateErrors] = useState<Record<string, string>>({});
  /** The phases of an assignment in flight, in arrival order. Empty until one is asked for. */
  const [proposalSteps, setProposalSteps] = useState<AssignmentStep[]>([]);
  const [proposal, setProposal] = useState<ProposedMeasurements | null>(null);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  /** Whether the crop-inspection list is open. Closed by default; it is the slow route. */
  const [inspecting, setInspecting] = useState(false);
  /** Review by exception (admin, 2026-10-06): show only the fields that need a look. */
  const [onlyNeeded, setOnlyNeeded] = useState(true);
  /** Bumped to re-fetch the form while the reader is still working. */
  const [reload, setReload] = useState(0);
  // Decisions on parts, which change the runs under each countertop (#893).
  const [partsDecided, setPartsDecided] = useState(0);
  // Readings confirmed here, which a part's width may then be linked to (#913).
  const [readingsConfirmed, setReadingsConfirmed] = useState(0);
  /**
   * Which fields a model proposed, by field key, with the readings it chose.
   *
   * **Nothing a model proposed reaches a reviewer unmarked.** A value that appeared in a box with
   * no explanation is indistinguishable from one a person read off the drawing, and the reviewer's
   * confirmation is the only thing standing between a proposal and a verdict. The mark is dropped
   * the moment they type in the field: it is theirs from then on.
   */
  const [aiFilled, setAiFilled] = useState<Record<string, string[]>>({});
  const reviewerEditedSinglesRef = useRef<Set<string>>(new Set());
  const reviewerEditedManyRef = useRef<Set<string>>(new Set());
  const resetPackageIdRef = useRef<string | undefined>(undefined);
  const loadedDataPackageIdRef = useRef<string | null>(null);
  const [refreshUnavailable, setRefreshUnavailable] = useState(false);
  /**
   * The wizard (#1061): which step is shown, and what each section reports as done/total. Every step
   * stays mounted — only hidden — so the drafts inside its sections survive a switch.
   */
  const [step, setStep] = useState<MeasureStep>('drawings');
  const rootRef = useRef<HTMLDivElement>(null);
  /** When the reviewer last moved between steps, and to which step focus should follow (#1124). */
  const stepChangedAtRef = useRef(0);
  const focusStepRef = useRef<MeasureStep | null>(null);
  const [sectionCounts, setSectionCounts] = useState<Record<string, StepCount | null>>({});
  const [unsavedRows, setUnsavedRows] = useState(0);
  const reportCount = (section: string) => (count: StepCount | null) =>
    setSectionCounts((prior) => (sameCount(prior[section], count) ? prior : { ...prior, [section]: count }));
  // Each section says whether it loaded empty, so a step's "nothing here" line is never shown over a
  // failed or still-loading section (#1124).
  const [sectionStates, setSectionStates] = useState<Record<string, SectionState>>({});
  const reportState = (section: string) => (state: SectionState) =>
    setSectionStates((prior) => (prior[section] === state ? prior : { ...prior, [section]: state }));
  // "Open countertop card" (Results, the queue) lands on the countertop rows: show their step first.
  const [seenTarget, setSeenTarget] = useState<string | null>(null);
  if (targetRow && targetRow !== seenTarget) {
    setSeenTarget(targetRow);
    setStep('runs');
  } else if (!targetRow && seenTarget !== null) {
    setSeenTarget(null);
  }
  // A step change moves focus to that step's heading, not the footer: a second click of a double-click
  // on Next then lands on nothing rather than on whatever button took Next's place (#1124).
  useEffect(() => {
    if (focusStepRef.current !== step) return;
    focusStepRef.current = null;
    rootRef.current?.querySelector<HTMLElement>(`[data-step-heading="${step}"]`)?.focus({ preventScroll: true });
  }, [step]);
  // Starts at 0, so a request that arrives with the first mount (the header opened this tab) counts.
  const [seenRunRequest, setSeenRunRequest] = useState(0);
  if ((runChecksRequest ?? 0) !== seenRunRequest) {
    setSeenRunRequest(runChecksRequest ?? 0);
    if (runChecksRequest) setStep('settings');
  }

  useEffect(() => {
    let cancelled = false;
    // Polling refreshes the data for the current package. Clearing the contract here would replace
    // the whole Measure screen with its loading state, unmounting placement, run, link, and value
    // editors while a reviewer is working. Only a real package switch resets that local work.
    if (packageChanged(resetPackageIdRef.current, selectedPackageId)) {
      resetPackageIdRef.current = selectedPackageId;
      loadedDataPackageIdRef.current = null;
      setPackageId('');
      setLoadError(null);
      setRefreshUnavailable(false);
      setNeeded(null);
      setSelectedPageNumber(1);
      setRuns({});
      setSingles({});
      const freshEdits = new Set<string>();
      reviewerEditedSinglesRef.current = freshEdits;
      setReviewerEditedSingles(freshEdits);
      reviewerEditedManyRef.current = new Set();
      setReviewerEditedMany(new Set());
      setChoices({});
      setSourceChoices({});
      setReferences({});
      setDeclinedCitations({});
      setCandidates([]);
      setSemanticTypes([]);
      setCandidateLoadError(null);
      setVocabularyLoadError(null);
      setCandidateError(null);
      setCandidateChoices({});
      setCandidateErrors({});
      setProposalSteps([]);
      setProposal(null);
      setProposalError(null);
      setAiFilled({});
    }
    const applyRequiredInputs = (required: Needed) => {
      const pageConfirmed = required.confirmed_readings.filter(
        (reading) => reading.page_index === selectedPageNumber - 1,
      );
      const confirmedByKey = pageConfirmed.reduce<Record<string, string[]>>(
        (grouped, reading) => ({
          ...grouped,
          [reading.key]: [...(grouped[reading.key] ?? []), reading.value],
        }),
        {},
      );
      const nextMarks: Record<string, string[]> = {};

      setSingles((prior) => {
        const next = { ...prior };
        for (const quantity of required.quantities.filter((q) => !q.many)) {
          const confirmed = confirmedByKey[quantity.key] ?? [];
          if (
            confirmed.length === 1 &&
            !reviewerEditedSinglesRef.current.has(quantity.key) &&
            !(next[quantity.key] ?? '').trim()
          ) {
            next[quantity.key] = confirmed[0];
          }
        }
        for (const field of required.proposed_readings ?? []) {
          if (field.many) continue;
          const pageValues = field.values.filter(
            (reading) => reading.page_index === selectedPageNumber - 1,
          );
          const values = pageValues.map((reading) => reading.value);
          if (!values.length) continue;
          if (confirmedByKey[field.field_key]?.length) continue;
          if (reviewerEditedSinglesRef.current.has(field.field_key)) continue;
          if ((next[field.field_key] ?? '').trim()) continue;
          next[field.field_key] = values[0];
          nextMarks[field.field_key] = pageValues.map((reading) => reading.candidate_id);
        }
        return next;
      });

      setRuns((prior) => {
        const next = { ...prior };
        for (const quantity of required.quantities.filter((q) => q.many)) {
          if (reviewerEditedManyRef.current.has(quantity.key)) continue;
          const existing = next[quantity.key] ?? [''];
          if (existing.some((value) => value.trim())) continue;
          const confirmed = (confirmedByKey[quantity.key] ?? []).filter((value) => value.trim());
          next[quantity.key] = confirmed.length ? confirmed : [''];
        }
        for (const field of required.proposed_readings ?? []) {
          if (!field.many) continue;
          if (reviewerEditedManyRef.current.has(field.field_key)) continue;
          const pageValues = field.values.filter(
            (reading) => reading.page_index === selectedPageNumber - 1,
          );
          if (!pageValues.length) continue;
          if (confirmedByKey[field.field_key]?.length) continue;
          if ((next[field.field_key] ?? []).some((value) => value.trim())) continue;
          const expectedCount = field.expected_count ?? pageValues.length;
          const values = proposalValuesAtPositions(pageValues, expectedCount);
          next[field.field_key] = values;
          nextMarks[field.field_key] = pageValues.map((reading) => reading.candidate_id);
        }
        return next;
      });

      setAiFilled((prior) => ({ ...prior, ...nextMarks }));
      setChoices((prior) => {
        const defaults = layoutChoiceDefaults(required.discriminators);
        if (Object.keys(defaults).length === 0) return prior;
        return { ...defaults, ...prior };
      });
    };

    const load = async () => {
      try {
        if (!selectedPackageId) return;
        const fields = await getRequiredInputs(projectId(), selectedPackageId, selectedPageNumber);
        if (cancelled) return;
        const required = fields as unknown as Needed;
        if (
          required.page_numbers.length > 0 &&
          !required.page_numbers.includes(selectedPageNumber)
        ) {
          setSelectedPageNumber(required.page_numbers[0]);
          setCandidates([]);
        } else if (required.page_numbers.includes(selectedPageNumber)) {
          // Optional readings must remain page-scoped. A refused page is not a partial list, and
          // must not hide the required form or discard a reviewer's in-progress values.
          void listCandidates(projectId(), selectedPackageId, selectedPageNumber).then(
            (read) => { if (!cancelled) { setCandidates(read.candidates); setCandidateLoadError(null); } },
            (caught: unknown) => { if (!cancelled) { setCandidates([]); setCandidateLoadError(caught instanceof ApiError ? caught.message : String(caught)); } },
          );
        } else {
          // Pages are written asynchronously after upload. Keep showing the in-progress state and
          // poll the contract; requesting an unknown page would be a correct 404, not an empty list.
          setCandidates([]);
        }
        setPackageId(selectedPackageId);
        loadedDataPackageIdRef.current = selectedPackageId;
        setNeeded(required);
        setLoadError(null);
        setRefreshUnavailable(false);
        applyRequiredInputs(required);
        // Reading suggestions and vocabulary are helpful, not prerequisites for typing a value.
        // A failed optional request must not hide or reset the current form and its drafts.
        void listSemanticTypes().then(
          (vocabulary) => { if (!cancelled) { setSemanticTypes(vocabulary); setVocabularyLoadError(null); } },
          (caught: unknown) => { if (!cancelled) setVocabularyLoadError(caught instanceof ApiError ? caught.message : String(caught)); },
        );
      } catch (caught) {
        if (!cancelled) {
          // A transient poll failure must not replace the loaded form with the fatal load state:
          // doing so unmounts every Measure editor and loses the reviewer's local draft. Initial
          // loads and package switches still fail closed because there is no loaded form to keep.
          if (!refreshFailureIsFatal(loadedDataPackageIdRef.current, selectedPackageId)) {
            setRefreshUnavailable(true);
          } else {
            setLoadError(caught instanceof ApiError ? caught.message : String(caught));
          }
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // Re-fetch only when the selected package changes, never on a keystroke within its form.
  }, [selectedPackageId, selectedPageNumber, reload, resourceRetry]);

  /**
   * Go back and look while the drawings are still being read.
   *
   * **The form loaded once, and reading a drawing takes the better part of a minute.** Opening
   * Measure straight after uploading therefore showed "nothing was read off these drawings" — and
   * kept showing it, because nothing went back to look. The readings landed thirty seconds later
   * and the page never knew. Reported three times as "the AI is not filling anything"; the values
   * were in the database the whole time.
   *
   * Only while the pipeline says it is still working, so a package it has finished with is not
   * polled for ever. Five seconds because that is fast enough that nobody sits watching an empty
   * form, and slow enough that a reviewer reading the page is not re-fetching it twelve times a
   * minute.
   */
  useEffect(() => {
    if (!needed?.still_reading) return;
    const timer = window.setInterval(() => setReload((count) => count + 1), REQUIRED_INPUTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [needed?.still_reading]);

  if (!selectedPackageId) {
    return (
      <div data-tw data-slot="measure-wizard" className="flex h-full min-h-0 flex-1 flex-col items-start gap-3 overflow-y-auto bg-background p-4 font-sans text-foreground sm:p-6">
        <h2 className="text-xl font-semibold tracking-tight">Choose a review first</h2>
        <p className="text-sm text-muted-foreground">Measurements belong to one uploaded drawing pair: open it from Documents.</p>
        {onChoosePackage && (
          <Button type="button" onClick={onChoosePackage}>
            Open documents
          </Button>
        )}
      </div>
    );
  }

  const markManyEdited = (key: string) => {
    const next = new Set(reviewerEditedManyRef.current).add(key);
    reviewerEditedManyRef.current = next;
    setReviewerEditedMany(next);
  };

  const setRun = (key: string, index: number, value: string) => {
    markManyEdited(key);
    setRuns((prior) => ({
      ...prior,
      [key]: (prior[key] ?? ['']).map((v, i) => (i === index ? value : v)),
    }));
  };

  async function confirmFromMeasure(candidate: CandidateOut, semanticType: string) {
    if (!packageId || !needed || !candidate.source || !candidate.value) return;
    const target = needed.quantities.find(
      (quantity) => quantity.key === `${candidate.source}:${semanticType}`,
    );
    if (!target) {
      const message = `${semanticType} is not a measurement this published rulebook asks for from this document.`;
      setCandidateError(message);
      setCandidateErrors((current) => ({ ...current, [candidate.candidate_id]: message }));
      return;
    }

    setConfirming(candidate.candidate_id);
    setCandidateError(null);
    setCandidateErrors((current) => {
      const next = { ...current };
      delete next[candidate.candidate_id];
      return next;
    });
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
        page_index: candidate.page_index,
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
          const readingCount =
            needed.confirmed_readings.filter((item) => item.key === key).length + 1;
          if (reviewerEditedSingles.has(target.key)) return prior;
          return { ...prior, [target.key]: readingCount === 1 ? reading.value : '' };
        });
      }
      setCandidates((current) =>
        current.filter((item) => item.candidate_id !== candidate.candidate_id),
      );
      setReadingsConfirmed((count) => count + 1);
    } catch (caught) {
      const message = caught instanceof ApiError ? caught.message : 'This drawing reading could not be confirmed.';
      setCandidateError(message);
      setCandidateErrors((current) => ({ ...current, [candidate.candidate_id]: message }));
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
    const confirmed = new Set<string>();
    for (const [key, candidateIds] of Object.entries(aiFilled)) {
      const quantity = needed.quantities.find((item) => item.key === key);
      if (!quantity || candidateIds.length === 0) continue;
      const proposal = needed.proposed_readings.find((item) => item.field_key === key);
      // Sparse form proposals remain editable suggestions. Do not confirm them as a complete
      // evidence list; the reviewer can fill the blank positions first, and the confirmed run
      // resolver still requires a width link for every member.
      if (
        quantity.many &&
        proposal?.expected_count != null &&
        !proposalCoversEveryPosition(proposal.values, proposal.expected_count)
      ) {
        continue;
      }
      try {
        // In order: a many-valued field's readings are a run, and the evidence path orders a run by
        // the time its readings were confirmed.
        for (const candidateId of candidateIds) {
          await confirmCandidate(projectId(), packageId, candidateId, quantity.semantic_type);
        }
        confirmed.add(key);
      } catch {
        // **A confirmation that fails costs the crop, never the value.** The number is still what
        // the reviewer accepted, so it goes down the typed path as before and the check still runs.
        // Losing a value because its provenance could not be recorded would be the worse trade.
      }
    }
    return confirmed;
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
      const result = await proposeMeasurements(projectId(), packageId, selectedPageNumber, (step) =>
        setProposalSteps((prior) => [...prior, step]),
      );
      setProposal(result);

      const filledSingles: Record<string, string> = {};
      const filledRuns: Record<string, string[]> = {};
      const marks: Record<string, string[]> = {};
      for (const assignment of result.assignments) {
        const expectedCount = assignment.expected_count ?? assignment.values.length;
        const values = assignment.many
          ? proposalValuesAtPositions(assignment.values, expectedCount)
          : assignment.values.map((reading) => reading.value);
        if (assignment.many) {
          if (reviewerEditedManyRef.current.has(assignment.field_key)) continue;
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
      if (await saveVisibleValues()) onValuesSaved?.();
    } finally {
      setBusy(false);
    }
  }

  async function onRunChecks() {
    if (!packageId || !needed) return;
    // A click this soon after a step change was aimed at the button that was there before (Next).
    if (Date.now() - stepChangedAtRef.current < RUN_CHECKS_SETTLE_MS) return;
    setBusy(true);
    setError(null);
    setAccepted(null);
    try {
      // A run must evaluate the values the reviewer can see, including any just-confirmed AI
      // proposals.  If parsing or storage fails, do not enqueue a stale or empty measurement set.
      if (!(await saveVisibleValues())) return;
      const response = await requestChecks(projectId(), packageId, choices);
      setAccepted(response.accepted_id);
      onChecksQueued?.();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (loadError && !needed) {
    return (
      <div data-tw data-slot="measure-wizard" className="flex h-full min-h-0 flex-1 flex-col items-start gap-3 overflow-y-auto bg-background p-4 font-sans text-foreground sm:p-6">
        <LoadError onRetry={() => setResourceRetry((count) => count + 1)} retryLabel="Try loading measurements again">
          {loadError}
        </LoadError>
      </div>
    );
  }
  if (!needed) {
    return (
      <div data-tw data-slot="measure-wizard" className="flex h-full min-h-0 flex-1 flex-col gap-3 overflow-y-auto bg-background p-4 font-sans text-foreground sm:p-6">
        <p role="status" className="text-sm text-muted-foreground">Reading what the rulebook needs…</p>
        <div className="h-16 w-full animate-pulse rounded-xl bg-muted motion-reduce:animate-none" aria-hidden="true" />
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
  /** Each reading the form could have been filled from, by id, with what the evidence path says. */
  const candidatesById = new Map<string, ReviewedCandidate & CandidateOut>(
    candidates.map((candidate) => [candidate.candidate_id, candidate as ReviewedCandidate & CandidateOut]),
  );
  /** Whether the reviewer needs to look at this field, and why — never whether its value is right. */
  const reviewOf = (quantity: Quantity): FieldReview =>
    fieldReview({
      hasValue: hasValue(quantity),
      reviewerOwned:
        (quantity.many ? reviewerEditedMany.has(quantity.key) : reviewerEditedSingles.has(quantity.key)) ||
        (readingsByKey[quantity.key] ?? []).length > 0,
      proposedCandidateIds: aiFilled[quantity.key] ?? null,
      candidates: candidatesById,
      placementUnverified: unverifiedFields.has(quantity.key),
    });
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
  ): 'empty' | 'proposed' | 'unplaced' | 'confirmed' | 'tagged' | 'typed' => {
    const readings = readingsByKey[quantity.key] ?? [];
    return measurementValueOrigin({
      hasValue: hasValue(quantity),
      humanEdited: quantity.many ? reviewerEditedMany.has(quantity.key) : reviewerEditedSingles.has(quantity.key),
      proposed: quantity.key in aiFilled,
      placementUnverified: unverifiedFields.has(quantity.key),
      qualifications: readings.map((reading) => reading.qualification),
    });
  };

  /** How many fields the filed proposal covers. Zero when nothing was filed, which is a real
   *  outcome: the checks refused the model's answer, or the reader attached nothing to fill from. */
  const storedProposalCount = (needed.proposed_readings ?? []).length;

  /** Fields whose placement nothing could confirm, because the drawing carries no line-work. */
  const unverifiedFields = new Set(
    (needed.proposed_readings ?? [])
      .filter((field) => !field.placement_verified)
      .map((field) => field.field_key),
  );
  const reviewSummary = reviewCounts(needed.quantities.map(reviewOf));

  /** Whether a proposal has been asked for at all. What decides who owns the panel's space. */
  const attempted = proposing || proposalSteps.length > 0 || proposal !== null || proposalError !== null;

  /** The sheets this rulebook reads from, in the order the fields appear. One group per sheet. */
  const sheets = needed.quantities.reduce<string[]>(
    (seen, quantity) => (seen.includes(quantity.source) ? seen : [...seen, quantity.source]),
    [],
  );
  const fieldDimensionKey = distributionFieldWidthKey(needed.quantities);

  // The step bar's numbers (#1061). Steps 1–2 add up what their sections report. Values counts the
  // settled fields — the reviewer's own, or both AI readers agreed — not merely filled ones, so an
  // AI fill still needing a look never earns a check mark. Settings counts typed ones and blank ones
  // with a rulebook value (blank means "use it"; the line under it says that value is a stand-in);
  // a blocked one waits on the vendor and is not the reviewer's to fill.
  const fillableSettings = needed.parameters.filter((parameter) => !parameter.blocked);
  const stepCounts: Record<MeasureStep, StepCount | null> = {
    drawings: sumCounts([sectionCounts.roles, sectionCounts.parts]),
    runs: sumCounts([sectionCounts.runs, sectionCounts.rows, sectionCounts.links]),
    values: { done: reviewSummary.done + reviewSummary.agreed, total: measurementFieldCount },
    settings: {
      done: fillableSettings.filter((parameter) => (singles[parameter.name] ?? '').trim() !== '' || Boolean(parameter.declared_default)).length,
      total: fillableSettings.length,
    },
  };
  // What pressing Save / Run checks records beyond what is typed, said where the buttons are (#1061):
  // they are reachable from every step now, not only below the values and layout they record.
  const aiValuesToConfirm = Object.keys(aiFilled).filter((key) => needed.quantities.some((quantity) => quantity.key === key));
  const aiUnchecked = aiValuesToConfirm.filter((key) => unverifiedFields.has(key)).length;
  const layoutAnswers = Object.values(choices).filter((choice) => choice !== '').length;
  const previousStep = stepBefore(step);
  const nextStep = stepAfter(step);
  const goToStep = (target: MeasureStep) => {
    stepChangedAtRef.current = Date.now();
    focusStepRef.current = target;
    setStep(target);
    if (rootRef.current) rootRef.current.scrollTop = 0;
  };

  /** One field of the form: its name and where its value came from | its value, then the evidence. */
  const renderField = (quantity: Quantity) => {
    const origin = fieldOrigin(quantity);
    const review = reviewOf(quantity);
    const pictured =
      review.state === 'needs_look'
        ? (aiFilled[quantity.key] ?? [])
            .map((id) => candidatesById.get(id))
            .filter((candidate): candidate is ReviewedCandidate & CandidateOut => candidate !== undefined)
        : [];
    return (
      <div className="flex flex-col gap-2 px-4 py-3" key={quantity.key} data-origin={origin} data-review={review.state}>
        {/* A compact row (#1061): the field and where its value came from | its value. The reason,
            crops, the AI's readings and the hints follow underneath. */}
        <div className="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
          <div className="flex min-w-0 flex-col gap-1.5">
            <label className="flex flex-wrap items-baseline gap-x-2" htmlFor={`q-${quantity.key}`}>
              {/* The rulebook's readable name leads; the code follows it. A reviewer filling this in
                  needs to know it is the sink cabinet width — `CT004` is what they quote back. */}
              <span className="text-sm font-medium">{fieldLabel(quantity)}</span>
              <span className="num text-xs text-muted-foreground">{quantity.semantic_type}</span>
            </label>
            <div className="flex flex-wrap items-center gap-1.5" id={`value-chips-${quantity.key}`} data-part="value-chips">
              <ReviewChip state={review.state} />
              {/* With no value, the state chip says it; one "needs a value" is enough. */}
              {origin !== 'empty' && origin !== 'typed' && origin !== 'confirmed' && (
                <Badge variant="outline" className="font-normal" data-origin-chip={origin}>
                  {ORIGIN_LABEL[origin]}
                </Badge>
              )}
              {(origin === 'typed' || origin === 'confirmed') && (
                <span className="text-xs text-muted-foreground">{ORIGIN_LABEL[origin]}</span>
              )}
              {quantity.key in aiFilled && (
                <span className="text-xs text-muted-foreground">
                  <span className="num">{aiFilled[quantity.key].length}</span> {aiFilled[quantity.key].length === 1 ? 'reading' : 'readings'} from the{' '}
                  {SOURCE_LABEL[quantity.source] ?? quantity.source}
                </span>
              )}
              <span className="text-xs text-muted-foreground" title={quantity.consumers.map((c) => c.rule_id).join(', ')}>
                feeds <span className="num">{quantity.consumers.length}</span> {quantity.consumers.length === 1 ? 'check' : 'checks'}
              </span>
            </div>
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            {quantity.many ? (
              <>
                {(runs[quantity.key] ?? ['']).map((value, index) => (
                  <div className="flex items-center gap-2" key={index}>
                    {isCategorical(quantity) ? (
                      <select
                        className={SELECT_CLASS}
                        id={index === 0 ? `q-${quantity.key}` : undefined}
                        aria-describedby={`value-chips-${quantity.key}`}
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
                        className={cn(INPUT_CLASS, 'num')}
                        id={index === 0 ? `q-${quantity.key}` : undefined}
                        aria-describedby={`value-chips-${quantity.key}`}
                        aria-label={`${fieldLabel(quantity)}, item ${index + 1}, left to right`}
                        placeholder={'25 1/2" or 648 mm'}
                        value={value}
                        onChange={(e) => {
                          releaseField(quantity.key);
                          setRun(quantity.key, index, e.target.value);
                        }}
                      />
                    )}
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove item ${index + 1} from ${fieldLabel(quantity)}`}
                      onClick={() => {
                        releaseField(quantity.key);
                        markManyEdited(quantity.key);
                        setRuns((prior) => ({
                          ...prior,
                          [quantity.key]: (prior[quantity.key] ?? []).filter((_, i) => i !== index),
                        }));
                      }}
                    >
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </div>
                ))}
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      markManyEdited(quantity.key);
                      setRuns((prior) => ({
                        ...prior,
                        [quantity.key]: [...(prior[quantity.key] ?? []), ''],
                      }));
                    }}
                  >
                    <Plus aria-hidden="true" /> Add another
                  </Button>
                  <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    Left to right, in drawing order.
                    <InfoTip label="Why the order matters">
                      <p>Two runs are compared position by position, so keep the drawing&apos;s left-to-right order.</p>
                    </InfoTip>
                  </span>
                </div>
                {needed.proposed_readings
                  .filter((field) => field.field_key === quantity.key && field.expected_count != null)
                  .map((field) => {
                    const pageValues = field.values.filter(
                      (reading) => reading.page_index === selectedPageNumber - 1,
                    );
                    if (pageValues.length >= (field.expected_count ?? 0)) return null;
                    return (
                      <Caution key={field.field_key}>
                        <span className="num">{pageValues.length}</span> of <span className="num">{field.expected_count}</span> piece widths were read: fill the blank positions before running the width check.
                      </Caution>
                    );
                  })}
              </>
            ) : (
              <input
                className={cn(INPUT_CLASS, 'num')}
                id={`q-${quantity.key}`}
                aria-describedby={`value-chips-${quantity.key}`}
                placeholder={'25 1/2" or 648 mm'}
                value={singles[quantity.key] ?? ''}
                onChange={(e) => {
                  releaseField(quantity.key);
                  const nextEdits = new Set(reviewerEditedSinglesRef.current).add(quantity.key);
                  reviewerEditedSinglesRef.current = nextEdits;
                  setReviewerEditedSingles(nextEdits);
                  setSingles((prior) => ({ ...prior, [quantity.key]: e.target.value }));
                }}
              />
            )}
          </div>
        </div>
        {review.reason && review.state !== 'empty' && <Hint>{review.reason}</Hint>}
        {pictured.length > 0 && packageId && (
          <div className="flex flex-wrap gap-2">
            {pictured.map((candidate) => (
              <MeasureCandidateCrop key={candidate.candidate_id} candidate={candidate} packageId={packageId} />
            ))}
          </div>
        )}
        {/* **The AI's own readings, offered at the field that wants one.** Confirming a reading is one
            click at the point it is needed, and the meaning is the field it was clicked under rather
            than a code chosen from a list. Nothing is filled in automatically. */}
        {review.state !== 'agreed' && (
          <AiReadings
            quantity={quantity}
            candidates={candidates}
            busyId={confirming}
            onUse={(candidate) => void confirmFromMeasure(candidate, quantity.semantic_type)}
          />
        )}
        {/* An empty field says which sheet to read it from: the rulebook knows, and says so. */}
        {origin === 'empty' && (
          <Hint>{quantity.source === 'USER_INPUT' ? 'Measure it on site.' : `Read it off the ${SOURCE_LABEL[quantity.source] ?? quantity.source}.`}</Hint>
        )}
      </div>
    );
  };

  const settingsCount = stepCounts.settings;
  const pages = needed.page_numbers.length ? needed.page_numbers : [selectedPageNumber];

  return (
    // The wizard is its own scroll area; the action bar sticks to its bottom edge (#1124).
    <div
      ref={rootRef}
      data-tw
      data-slot="measure-wizard"
      className="h-full min-h-0 w-full flex-1 overflow-y-auto bg-background font-sans text-foreground"
    >
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-4 pt-5 sm:px-6">
        <header className="flex flex-col gap-1">
          <h2 className="text-xl font-semibold tracking-tight">Review measurements</h2>
          {/* One line; the rest is behind "?" (#1061). */}
          <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-muted-foreground">
            <span>
              Type every value with its unit: <span className="num text-foreground">25 1/2&quot;</span> or{' '}
              <span className="num text-foreground">648 mm</span>.
            </span>
            <InfoTip label="How these values are used">
              <p>Work through the four steps, then run the checks. Check suggested values against the drawing, confirm what each one measures, then fill what is missing.</p>
              <p>A suggestion is not a confirmed measurement. Values without units are refused. The {needed.rules_published} published rules compare saved values exactly; missing or uncertain inputs can leave a check undecided.</p>
              <p><strong>Save values</strong> records the values and settings (steps 3 and 4) without running checks. <strong>Run checks</strong> saves them first and queues checks only if saving succeeds.</p>
              <p>Drawings, parts, countertop rows, walls, runs and readings (steps 1 and 2) are saved by their own buttons.</p>
            </InfoTip>
          </p>
          {refreshUnavailable && (
            <Hint role="status">Could not refresh the drawing data just now; your unsaved values are still here and the page will try again.</Hint>
          )}
        </header>

        <MeasurementSectionNav current={step} counts={stepCounts} onPick={goToStep} />
        {loadError && (
          <LoadError onRetry={() => setResourceRetry((count) => count + 1)}>
            The latest refresh failed; your entered values are still here. {loadError}
          </LoadError>
        )}

        {/* **Before any reading can fill a field on a combined sheet** (#795): a reading is used only on
            the side of the drawing it sits in, so the drawings' roles come first. Nothing renders for
            a package of two separate PDFs. Each answer re-reads the readings, which now have a side. */}
        <div data-measure-step="drawings" hidden={step !== 'drawings'}>
          <StepHeading step="drawings" />
          <div id="measure-drawings" className="flex scroll-mt-4 flex-col gap-8">
            {packageId && (
              <DrawingRoles
                key={`${packageId}-drawing-roles`}
                packageId={packageId}
                onConfirmed={() => setReload((count) => count + 1)}
                onProgress={reportCount('roles')}
                onState={reportState('roles')}
              />
            )}
            {/* **The parts of each vendor drawing** (#882): suggested, and each one decided by a person. */}
            {packageId && (
              <DrawingParts
                key={`${packageId}-drawing-parts`}
                packageId={packageId}
                refresh={reload}
                onDecided={() => setPartsDecided((count) => count + 1)}
                onProgress={reportCount('parts')}
                onState={reportState('parts')}
              />
            )}
            {stepIsEmpty([sectionStates.roles, sectionStates.parts]) && <EmptyStep>Nothing to decide on these drawings.</EmptyStep>}
          </div>
        </div>

        {/* **Which parts sit under each countertop** (#893): suggested from the confirmed parts above,
            and each countertop's run decided by a person. Read again whenever a part is decided. */}
        <div data-measure-step="runs" hidden={step !== 'runs'}>
          <StepHeading step="runs" />
          <div id="measure-runs" className="flex scroll-mt-4 flex-col gap-8">
            {packageId && (
              <CountertopRuns key={`${packageId}-countertop-runs`} packageId={packageId} refresh={reload + partsDecided} onProgress={reportCount('runs')} onState={reportState('runs')} />
            )}
            {packageId && (
              <SlotReaderRows packageId={packageId} refresh={reload + partsDecided + readingsConfirmed} targetRow={targetRow} onTargetReached={onTargetReached} onReviewRow={onReviewRow} onOpenQueue={onOpenQueue} onProgress={reportCount('rows')} onUnsaved={setUnsavedRows} onState={reportState('rows')} />
            )}
            {/* **Which reading is each part's width** (#913): suggested from the confirmed parts and
                readings, and each part's link decided by a person. Read again whenever a part is decided
                or a reading is confirmed here. */}
            {packageId && (
              <ReadingParts
                key={`${packageId}-reading-parts`}
                packageId={packageId}
                refresh={reload + partsDecided + readingsConfirmed}
                onProgress={reportCount('links')}
                onState={reportState('links')}
              />
            )}
            {stepIsEmpty([sectionStates.runs, sectionStates.rows, sectionStates.links]) && <EmptyStep>No countertop to confirm yet.</EmptyStep>}
          </div>
        </div>

        {/* A plain wrapper hides the step: the grid inside would override `hidden` (#1061). */}
        <div data-measure-step="values" hidden={step !== 'values'}>
          <StepHeading step="values" />
          <section
            id="measure-values"
            aria-labelledby="measure-values-title"
            className="grid scroll-mt-4 gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:items-start"
          >
            {/* The page being reviewed beside the form (admin, 2026-10-06): the picture first. */}
            <div className="flex min-w-0 flex-col gap-3 lg:sticky lg:top-4">
              {packageId && <PageDrawing packageId={packageId} pageNumber={selectedPageNumber} />}
              <div className="flex items-center gap-2">
                <label htmlFor="measure-page-number" className="shrink-0 text-sm font-medium">Review one page</label>
                <select
                  id="measure-page-number"
                  className={cn(SELECT_CLASS, 'w-auto min-w-28')}
                  value={selectedPageNumber}
                  onChange={(event) => {
                    setCandidates([]);
                    setProposalSteps([]);
                    setProposal(null);
                    setProposalError(null);
                    setSelectedPageNumber(Number(event.currentTarget.value));
                  }}
                >
                  {pages.map((page) => (
                    <option key={page} value={page}>Page {page}</option>
                  ))}
                </select>
                <InfoTip label="About the page picker">
                  <p>Readings and Fill with AI are limited to this page. The values you type belong to the whole set and are all saved together.</p>
                </InfoTip>
              </div>
            </div>

            <div className="flex min-w-0 flex-col gap-5">
              {candidateLoadError && (
                <LoadError onRetry={() => setResourceRetry((count) => count + 1)}>
                  Drawing readings could not be refreshed; you can still enter values. {candidateLoadError}
                </LoadError>
              )}
              {vocabularyLoadError && (
                <LoadError onRetry={() => setResourceRetry((count) => count + 1)}>
                  Reading meanings could not be loaded; you can still enter values. {vocabularyLoadError}
                </LoadError>
              )}

              {/* **One headline, one bar, one table.** The bar is the one number that answers "is there
                  anything left to do?" — labelled as coverage, because a filled field is not a right one. */}
              <div data-slot="measure-coverage" aria-label="How much of the form is filled" className="flex flex-col gap-3 rounded-xl border bg-card p-4">
                <div className="flex items-baseline justify-between gap-3">
                  <h3 id="measure-values-title" className="text-base font-semibold">
                    Values · page <span className="num">{selectedPageNumber}</span>
                  </h3>
                </div>
                <p className="text-sm">
                  <span className="num text-2xl font-semibold">{filledFieldCount}</span> of{' '}
                  <span className="num">{measurementFieldCount}</span> fields have a value
                </p>
                <Progress value={coveragePercent} aria-label="Fields with a value" />
                <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  {needed.still_reading && candidates.length + confirmedCount === 0 ? (
                    <span>Reading dimensions from the drawings</span>
                  ) : (
                    <span><span className="num text-foreground">{candidates.length + confirmedCount}</span> read off the drawings</span>
                  )}
                  <span><span className="num text-foreground">{confirmedCount}</span> confirmed</span>
                  <span><span className="num text-foreground">{candidates.length}</span> without a meaning</span>
                  {exactTagFieldCount > 0 && (
                    <span><span className="num text-foreground">{exactTagFieldCount}</span> filled by the drawing&apos;s own tags</span>
                  )}
                  <InfoTip label="About coverage">
                    <p>Coverage, not accuracy: a filled field is not a right one.</p>
                    <p>A dimension the reader could not parse, or a number with no unit, is not counted here at all — it was refused rather than guessed, and the field stays empty for you.</p>
                    {exactTagFieldCount > 0 && <p>Fields filled by the drawing&apos;s own tags needed no click: the drawing states the meaning itself.</p>}
                  </InfoTip>
                </p>
                {/* The fields as a sheet × state table (#1061): the numbers first. */}
                <div className="min-w-0 overflow-x-auto">
                  <table className="w-full text-left text-xs" data-part="coverage-table">
                    <caption className="sr-only">Fields by drawing and state</caption>
                    <thead className="text-muted-foreground">
                      <tr>
                        <th scope="col" className="py-1 pr-2 font-normal">Drawing</th>
                        <th scope="col" className="px-1 py-1 text-right font-normal">Needs a value</th>
                        <th scope="col" className="px-1 py-1 text-right font-normal">Needs a look</th>
                        <th scope="col" className="px-1 py-1 text-right font-normal">Agreed</th>
                        <th scope="col" className="py-1 pl-1 text-right font-normal">Done</th>
                      </tr>
                    </thead>
                    <tbody className="num">
                      {sheets.map((sheet) => {
                        const states = needed.quantities.filter((quantity) => quantity.source === sheet).map((quantity) => reviewOf(quantity).state);
                        const count = (state: FieldReview['state']) => states.filter((value) => value === state).length;
                        return (
                          <tr key={sheet} className="border-t">
                            <th scope="row" className="py-1 pr-2 font-sans font-normal">{sentence(SOURCE_LABEL[sheet] ?? sheet)}</th>
                            <td className="px-1 py-1 text-right">{count('empty')}</td>
                            <td className="px-1 py-1 text-right">{count('needs_look')}</td>
                            <td className="px-1 py-1 text-right">{count('agreed')}</td>
                            <td className="py-1 pl-1 text-right">{count('done')}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* **Filling the form from the drawings.**
                  The model is not asked what a number means in the abstract; it is asked to map readings
                  this pipeline has already located onto the fields the rulebook already names, and every
                  structural property of a right answer is one `workflow/assignment.py` verifies. What it
                  is never given is the rule arithmetic — a model that knew the equation could choose
                  readings that make it balance, and the check would then confirm the balance on a drawing
                  with a real error in it. */}
              {/* **The offer stands until an attempt has been made, and then the panel owns the space.**
                  Keyed on whether anything was attempted, so a failure before the first frame (a 413, a
                  404, a server that did not answer) is shown rather than swallowed. */}
              {needed.still_reading ? (
                <ReadingProgress state={needed.revision_state} />
              ) : !attempted && storedProposalCount > 0 ? (
                /* **Already done, before the reviewer arrived.** The proposal is made when the drawings
                   are read and filed, so this panel reports a completed step rather than offering one. */
                <div data-slot="measure-fill" data-state="done" className="flex flex-col gap-2 rounded-xl border bg-card p-4 sm:flex-row sm:items-start sm:justify-between">
                  <div className="flex min-w-0 flex-col gap-1">
                    <h4 className="flex items-center gap-2 text-sm font-semibold">
                      <Sparkles className="size-4" aria-hidden="true" /> Filled from the drawings
                    </h4>
                    <p className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
                      <span>
                        <span className="num text-foreground">{storedProposalCount}</span> {storedProposalCount === 1 ? 'field' : 'fields'} proposed by AI: check, edit, then save.
                      </span>
                      <InfoTip label="About the AI fill">
                        <p>These were filled when the drawings were read, and every one passed the checks against the drawing — the right sheet, attached to a real dimension line, one reading per field, and a run in the order the drawing draws it.</p>
                        <p>Nothing is saved until you press Save.</p>
                      </InfoTip>
                    </p>
                    {/* Stays visible: these need a look at the crop before they are kept. */}
                    {unverifiedFields.size > 0 && (
                      <Caution>
                        <span className="num">{unverifiedFields.size}</span> not checked against the drawing&apos;s lines: open their crops before keeping them.{' '}
                        <InfoTip label="Why these could not be checked">
                          <p>These sheets are scanned images with no dimension line-work, so nothing confirmed that each number sits on the dimension it measures.</p>
                        </InfoTip>
                      </Caution>
                    )}
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="shrink-0 self-start"
                    onClick={() => void onPropose()}
                    disabled={busy || candidates.length === 0}
                  >
                    <Sparkles aria-hidden="true" /> Ask again
                  </Button>
                </div>
              ) : !attempted ? (
                <div data-slot="measure-fill" data-state="offer" className="flex flex-col gap-2 rounded-xl border bg-card p-4">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 flex-col gap-1">
                      <h4 className="text-sm font-semibold">Fill these from the drawings</h4>
                      <p className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
                        <span>AI proposes; nothing is saved until you save.</span>
                        <InfoTip label="About Fill with AI">
                          <p>A model proposes which reading fills which field when the drawings are read, so this form normally arrives already filled.</p>
                          <p>Every proposal is checked against the drawing — the right sheet, attached to a real dimension line, one reading per field, and a run in the order the drawing draws it — and refused as a batch if any part of it fails.</p>
                        </InfoTip>
                      </p>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="shrink-0 self-start"
                      onClick={() => void onPropose()}
                      disabled={busy || candidates.length === 0}
                    >
                      <Sparkles aria-hidden="true" /> Fill with AI
                    </Button>
                  </div>
                  {/* **Two situations, not one sentence covering both**: a finished package and a drawing
                      nothing was read from want completely different things done about them. */}
                  {candidates.length === 0 && (
                    <Hint>
                      {confirmedCount > 0
                        ? 'Nothing left to propose: every reading off these drawings already has a meaning.'
                        : 'Nothing was read off these drawings. Check on Documents that both finished uploading and were read.'}
                    </Hint>
                  )}
                </div>
              ) : (
                <AssignmentProgress steps={proposalSteps} result={proposal} error={proposalError} />
              )}
              {(proposal || proposalError) && !proposing && (
                <Button type="button" size="sm" variant="outline" className="self-start" onClick={() => void onPropose()}>
                  <Sparkles aria-hidden="true" /> Ask again
                </Button>
              )}

              {/* **Folded, because it is the slow route.** Every reading here is also offered beside the
                  field it could fill; this list is for the moment a reviewer wants to see the crop
                  before saying what a number means. */}
              {candidates.length > 0 && (
                <section data-slot="reading-inspector" aria-labelledby="ai-proposals-heading" className="rounded-xl border bg-card">
                  <button
                    type="button"
                    className="flex min-h-11 w-full items-center gap-2 px-4 py-2 text-left text-sm font-medium"
                    aria-expanded={inspecting}
                    id="ai-proposals-heading"
                    onClick={() => setInspecting((shown) => !shown)}
                  >
                    <ChevronRight className={cn('size-4 shrink-0 transition-transform motion-reduce:transition-none', inspecting && 'rotate-90')} aria-hidden="true" />
                    <span>
                      Inspect <span className="num">{candidates.length}</span> {candidates.length === 1 ? 'reading' : 'readings'} on the crop before using {candidates.length === 1 ? 'it' : 'them'}
                    </span>
                  </button>
                  {inspecting && (
                    <div className="grid gap-3 border-t p-4 sm:grid-cols-2">
                      {candidates.map((candidate) => {
                        const availableTypes = typesForCandidate(candidate);
                        const isConfirming = confirming === candidate.candidate_id;
                        return (
                          <div className="flex min-w-0 flex-col gap-2 rounded-lg border p-3" key={candidate.candidate_id}>
                            <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
                              <span className="num font-semibold">{candidate.value}</span>
                              <span className="text-xs text-muted-foreground">
                                {SOURCE_LABEL[candidate.source ?? ''] ?? candidate.source_refusal ?? 'drawing source unavailable'}
                              </span>
                              <span className="text-xs text-muted-foreground">page <span className="num">{candidate.page_index + 1}</span></span>
                              {/* No confidence score — it is written only by RapidOCR, which produced 903
                                  candidates and zero values on the real drawing. #720. */}
                            </p>
                            {candidate.crop_key && packageId ? (
                              <MeasureCandidateCrop candidate={candidate} packageId={packageId} />
                            ) : (
                              <Hint>No crop is available, so this reading cannot be confirmed here.</Hint>
                            )}
                            {/* **The codes are not a vocabulary anybody has.** The rulebook names every
                                quantity readably and the form uses those names on its own fields; this is
                                the same `fieldLabel`, so the dropdown and the form cannot call one quantity
                                two things. */}
                            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                              What does this number measure?
                              <select
                                className={SELECT_CLASS}
                                aria-label={`Meaning for AI reading ${candidate.value}`}
                                value={candidateChoices[candidate.candidate_id] ?? ''}
                                disabled={confirming !== null || !candidate.crop_key || availableTypes.length === 0}
                                onChange={(event) => {
                                  const type = event.target.value;
                                  setCandidateChoices((current) => ({ ...current, [candidate.candidate_id]: type }));
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
                            {isConfirming && <Hint role="status">Saving confirmation…</Hint>}
                            {candidateErrors[candidate.candidate_id] && (
                              <LoadError
                                onRetry={candidateChoices[candidate.candidate_id] && confirming === null ? () => void confirmFromMeasure(candidate, candidateChoices[candidate.candidate_id]) : undefined}
                                retryLabel="Try this confirmation again"
                              >
                                {candidateErrors[candidate.candidate_id]}
                              </LoadError>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {candidateError && <div className="border-t px-4 py-3"><LoadError>{candidateError}</LoadError></div>}
                </section>
              )}

              <div className="flex flex-col gap-2">
                <p className="text-xs text-muted-foreground" role="status">
                  {needed.confirmed_readings.length > 0 ? (
                    <>
                      <span className="num">{needed.confirmed_readings.length}</span> drawing {needed.confirmed_readings.length === 1 ? 'reading has' : 'readings have'} filled the fields below: review them before saving.
                    </>
                  ) : (
                    'No AI reading confirmed yet: choose a meaning above, or type a value you read.'
                  )}
                </p>
                {/* Review by exception (admin, 2026-10-06): show only the fields that need a look. */}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm" role="status">
                    <span className="num">{reviewSummary.agreed}</span> agreed by both AI readers ·{' '}
                    <span className="num">{reviewSummary.needs_look + reviewSummary.empty}</span> need a look
                    {reviewSummary.done > 0 && (
                      <>
                        {' '}· <span className="num">{reviewSummary.done}</span> done
                      </>
                    )}
                  </p>
                  <label className="flex min-h-8 items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="size-4 accent-foreground"
                      checked={onlyNeeded}
                      onChange={(event) => setOnlyNeeded(event.target.checked)}
                    />
                    Show only what needs a look
                  </label>
                </div>
              </div>

              {/* **Grouped by the sheet the value is read from**, so the shop drawing's fields are filled
                  with the shop drawing open, and the sheet is stated once as a heading. */}
              {sheets.map((sheet) => {
                const inSheet = needed.quantities.filter((quantity) => quantity.source === sheet);
                const done = inSheet.filter(hasValue).length;
                const shown = inSheet.filter((quantity) => {
                  const review = reviewOf(quantity);
                  // A field the reviewer typed into stays on screen: hiding it as "done" after the first
                  // keystroke took the box away mid-word (found in #1061). Settled ones still hide.
                  const typedHere = reviewerEditedSingles.has(quantity.key) || reviewerEditedMany.has(quantity.key);
                  return !(onlyNeeded && (review.state === 'agreed' || (review.state === 'done' && !typedHere)));
                });
                const headingId = `sheet-${sheet}`;
                return (
                  <section className="flex flex-col gap-2" key={sheet} aria-labelledby={headingId}>
                    <div className="flex items-baseline justify-between gap-3">
                      <h4 id={headingId} className="text-sm font-semibold">
                        {sentence(SOURCE_LABEL[sheet] ?? sheet)}
                      </h4>
                      <span className="text-xs text-muted-foreground">
                        <span className="num">{done}</span> of <span className="num">{inSheet.length}</span> filled
                      </span>
                    </div>
                    {shown.length === 0 ? (
                      <Hint className="rounded-xl border border-dashed px-4 py-3">Nothing here needs a look.</Hint>
                    ) : (
                      <div className="divide-y rounded-xl border bg-card">
                        {shown.map((quantity) => renderField(quantity))}
                      </div>
                    )}
                  </section>
                );
              })}
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
            </div>
          </section>
        </div>

        <div data-measure-step="settings" hidden={step !== 'settings'}>
          <StepHeading step="settings" />
          <div className="flex flex-col gap-8">
            <div id="measure-settings" className="scroll-mt-4">
              <StepSection
                id="measure-settings-title"
                slot="measure-settings"
                title="Settings"
                line={
                  settingsCount && settingsCount.total > 0 ? (
                    <strong className="font-medium text-foreground">
                      {settingsCount.done === settingsCount.total
                        ? `All ${countWords(settingsCount.total, 'setting')} set.`
                        : `${settingsCount.done} of ${countWords(settingsCount.total, 'setting')} set.`}
                    </strong>
                  ) : (
                    'Values for this job, not dimensions off a drawing.'
                  )
                }
                tipLabel="About settings"
                tip={
                  <>
                    <p>Values for this job, not dimensions off a drawing.</p>
                    <p>Where the rulebook suggests a value it is shown — a rule author&apos;s stand-in, not a number the client has confirmed. A blank setting with one uses it.</p>
                    <p>Where the architect&apos;s drawing states one, the page is shown but the number is not: type what you see, and it is saved only if it matches.</p>
                  </>
                }
              >
                <div className="divide-y rounded-xl border bg-card">
                  {needed.parameters.map((parameter) => {
                    const pointer = citingPointer(parameter, declinedCitations);
                    return (
                      <div className="grid gap-x-4 gap-y-2 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]" key={parameter.name}>
                        {/* A compact row (#1061): the setting | its value and where it came from. */}
                        <div className="flex min-w-0 flex-col gap-0.5">
                          <label className="text-sm font-medium" htmlFor={`p-${parameter.name}`}>
                            {sentence(parameter.name.replace(/_/g, ' '))}
                          </label>
                          <span className="text-xs text-muted-foreground">
                            {parameter.scope === 'run' ? 'This review only' : 'This project'}
                          </span>
                          <span className="num break-all text-xs text-muted-foreground">{parameter.name} · {parameter.rule_ids.join(', ')}</span>
                        </div>
                        <div className="flex min-w-0 flex-col gap-2">
                          {parameter.blocked ? (
                            <p className="flex items-start gap-1.5 text-sm text-muted-foreground" role="note">
                              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                              <span>
                                Waiting on the vendor, not a field to fill in.{' '}
                                <InfoTip label="Why this is waiting">
                                  <p>This check will report that it could not decide, which is the correct answer until the value arrives.</p>
                                </InfoTip>
                              </span>
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
                                  <SettingPassageCrop packageId={packageId} pointer={pointer} name={parameter.name} />
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
                                className={cn(INPUT_CLASS, 'num')}
                                id={`p-${parameter.name}`}
                                placeholder=""
                                value={singles[parameter.name] ?? ''}
                                onChange={(e) =>
                                  setSingles((prior) => ({ ...prior, [parameter.name]: e.target.value }))
                                }
                              />
                              {parameter.declared_default && (
                                <Hint>
                                  Blank uses the rulebook&apos;s <span className="num text-foreground">{parameter.declared_default}</span>, a stand-in the client has not confirmed.
                                </Hint>
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
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="ghost"
                                  className="self-start"
                                  onClick={() =>
                                    setDeclinedCitations((prior) => ({ ...prior, [parameter.name]: false }))
                                  }
                                >
                                  Type it from the architect&apos;s drawing, page {parameter.found.page_index + 1}
                                </Button>
                              )}
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </StepSection>
            </div>

            {needed.discriminators.length > 0 && (
              <StepSection
                id="measure-layout-title"
                slot="measure-layout"
                title="Layout"
                line="What the drawing shows; leave an uncertain layout unstated."
                tipLabel="About layout answers"
                tip={<p>A model-only suggestion stays unselected until you choose it; code-identified drawing clues may pre-select a value.</p>}
              >
                {/* Stays visible: a pre-selected answer is recorded when the checks run. */}
                {Object.keys(layoutChoiceDefaults(needed.discriminators)).length > 0 && (
                  <Caution>Run checks records the selected answers; change one first if the crop shows otherwise.</Caution>
                )}
                <div className="divide-y rounded-xl border bg-card">
                  {needed.discriminators.map((discriminator) => (
                    <div
                      className="grid gap-x-4 gap-y-2 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]"
                      data-proposed={discriminator.proposal ? 'true' : 'false'}
                      key={discriminator.name}
                    >
                      <div className="flex min-w-0 flex-col gap-1">
                        <label className="text-sm font-medium" htmlFor={`d-${discriminator.name}`}>
                          {sentence(discriminator.name.replace(/_/g, ' '))}
                        </label>
                        <span className="flex flex-wrap items-center gap-1.5">
                          <Badge variant="outline" className="font-normal">
                            {discriminator.proposal
                              ? discriminator.name === 'wall_config' && discriminator.proposal.requires_confirmation
                                ? 'Needs your confirmation'
                                : discriminator.proposal.prompt_id === 'slot-walls-drawing-clues-v1'
                                  ? 'Drawing clue'
                                  : 'Proposed'
                              : 'Needs you'}
                          </Badge>
                          <span className="num break-all text-xs text-muted-foreground">{discriminator.rule_ids.join(', ')}</span>
                        </span>
                      </div>
                      <div className="flex min-w-0 flex-col gap-2">
                        <select
                          className={SELECT_CLASS}
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
                        {discriminator.proposal ? (
                          <LayoutProposalCrop
                            packageId={packageId ?? ''}
                            proposal={discriminator.proposal}
                            discriminatorName={discriminator.name}
                          />
                        ) : (
                          <Hint>No proposed answer was recorded for this layout question.</Hint>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </StepSection>
            )}

            {stored.length > 0 && (
              <section aria-live="polite" aria-labelledby="measure-stored-title" className="flex flex-col gap-2">
                <h3 id="measure-stored-title" className="text-base font-semibold">Stored, as the system read them</h3>
                <ul className="num flex flex-col gap-1 rounded-xl border bg-card px-4 py-3 text-xs break-all">
                  {stored.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        </div>

        {/* **One primary action per step (#1124):** Next, and Run checks on the last step. Save values is
            the quiet one, on the steps whose values and settings it records. `#measure-run-checks` is
            where the review header's "Run checks" lands; asking for it opens the last step. */}
        <footer
          data-slot="measure-actions"
          className="sticky bottom-0 z-10 -mx-4 flex flex-col gap-2 border-t bg-background px-4 pt-3 pb-4 sm:-mx-6 sm:px-6 sm:pb-9"
        >
          {error && <LoadError>{error}</LoadError>}
          {accepted && (
            <p className="flex flex-wrap items-center gap-1.5 text-sm" role="status">
              <span>
                Checks queued (<span className="num">{accepted.slice(0, 8)}</span>). Results update when they have run.
              </span>
              <InfoTip label="About the queued checks">
                <p>The values and settings were saved first. A review worker now runs the deterministic checks; their results appear on the Results tab.</p>
              </InfoTip>
            </p>
          )}
          {stored.length > 0 && !error && !accepted && (
            <p className="text-sm text-muted-foreground" role="status">
              Saved <span className="num">{stored.length}</span> {stored.length === 1 ? 'value' : 'values'}.{' '}
              <button type="button" className="underline underline-offset-2" onClick={() => goToStep('settings')}>
                See what was stored
              </button>
            </p>
          )}
          {unsavedRows > 0 && (
            <p className="flex flex-wrap items-center gap-1.5 text-sm text-outcome-review-fg" role="status" data-part="unsaved-rows">
              <AlertTriangle className="size-4 shrink-0" aria-hidden="true" />
              <span>
                <span className="num">{unsavedRows}</span> countertop {unsavedRows === 1 ? 'row has' : 'rows have'} unsaved changes in step 2: use its &ldquo;Save this row&rdquo;.
              </span>
              <button type="button" className="text-foreground underline underline-offset-2" onClick={() => goToStep('runs')}>
                Go to step 2
              </button>
            </p>
          )}
          {(aiValuesToConfirm.length > 0 || layoutAnswers > 0) && (
            <p className="text-xs text-muted-foreground" data-part="save-records">
              {aiValuesToConfirm.length > 0 && (
                <>
                  Saving confirms <span className="num">{aiValuesToConfirm.length}</span> AI-filled {aiValuesToConfirm.length === 1 ? 'value' : 'values'}
                  {aiUnchecked > 0 && <> (<span className="num">{aiUnchecked}</span> not checked against the drawing&apos;s lines)</>}.{' '}
                </>
              )}
              {layoutAnswers > 0 && (
                <>
                  Run checks also records <span className="num">{layoutAnswers}</span> layout {layoutAnswers === 1 ? 'answer' : 'answers'}.{' '}
                </>
              )}
              <button type="button" className="text-foreground underline underline-offset-2" onClick={() => goToStep(aiValuesToConfirm.length > 0 ? 'values' : 'settings')}>
                Review {aiValuesToConfirm.length > 0 ? 'them' : 'the layout'}
              </button>
            </p>
          )}
          <div className="flex items-center gap-2">
            {previousStep && (
              <Button type="button" variant="ghost" onClick={() => goToStep(previousStep)}>
                <ChevronLeft aria-hidden="true" /> Back
              </Button>
            )}
            <span className="ml-auto" />
            {(step === 'values' || step === 'settings') && (
              <Button type="button" variant="outline" onClick={onSave} disabled={busy}>
                Save values
              </Button>
            )}
            {nextStep ? (
              // Keyed apart, so React never reuses the focused Next button as Run checks (#1124).
              <Button key="next" type="button" onClick={() => goToStep(nextStep)}>
                Next <ChevronRight aria-hidden="true" />
              </Button>
            ) : (
              <Button key="run-checks" type="button" id="measure-run-checks" onClick={onRunChecks} disabled={busy}>
                <Play aria-hidden="true" /> Run checks
              </Button>
            )}
          </div>
        </footer>
      </div>
    </div>
  );
}

/** "back offset minimum" → "Back offset minimum": a sentence-case label from a lower-case name. */
function sentence(words: string): string {
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Whether a field needs the reviewer, as a chip with its glyph and word (never colour alone): the
 * "needs your decision" amber only where a look is needed, the dashed "waiting on a value" where it
 * is empty. It says whether to look, never whether the value is right.
 */
const REVIEW_CHIP: Record<FieldReview['state'], { word: string; icon: typeof Check; tone: string }> = {
  agreed: { word: 'Both AI readers agreed', icon: CheckCircle2, tone: 'border-outcome-pass-fg/30 bg-outcome-pass-bg text-outcome-pass-fg' },
  needs_look: { word: 'Needs a look', icon: AlertCircle, tone: 'border-outcome-review-fg/60 bg-outcome-review-bg text-outcome-review-fg' },
  done: { word: 'Done', icon: Check, tone: 'border-border text-foreground' },
  empty: { word: 'Needs a value', icon: CircleDashed, tone: 'border-dashed border-outcome-missing-fg/60 bg-outcome-missing-bg text-outcome-missing-fg' },
};

function ReviewChip({ state }: { state: FieldReview['state'] }) {
  const chip = REVIEW_CHIP[state];
  const Icon = chip.icon;
  return (
    <span
      data-slot="field-review"
      data-state={state}
      className={cn('inline-flex w-fit items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap', chip.tone)}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      {chip.word}
    </span>
  );
}

/** A step with nothing in it says so, rather than showing an empty page. */
function EmptyStep({ children }: { children: ReactNode }) {
  return <p className="rounded-xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">{children}</p>;
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
    <div className="flex flex-col gap-2">
      {/* **Folded, because the same readings are offered under every field of their sheet.** Which
          sheet a number was read from is recorded; which field it belongs to is not — that is what
          the witness-line geometry in `extraction/geometry/dimension_lines.py` (#179) is being built
          to establish. Open, two readings and ten fields put the same values on screen twenty times. */}
      <button
        type="button"
        className="flex min-h-8 w-fit items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        <ChevronRight className={cn('size-3.5 transition-transform motion-reduce:transition-none', open && 'rotate-90')} aria-hidden="true" />
        <ScanLine className="size-3.5" aria-hidden="true" />
        <span>
          <span className="num">{offered.length}</span> {offered.length === 1 ? 'reading' : 'readings'} from the {sheet}
        </span>
      </button>
      {open && (
        <div className="flex flex-wrap gap-1.5">
          {offered.map((candidate) => (
            <Button
              key={candidate.candidate_id}
              type="button"
              size="sm"
              variant="outline"
              disabled={busyId !== null}
              onClick={() => onUse(candidate)}
              title={`Confirm ${candidate.value} as ${fieldLabel(quantity)}`}
            >
              <span className="num font-semibold">{candidate.value}</span>
              <span className="text-xs text-muted-foreground">p<span className="num">{candidate.page_index + 1}</span></span>
              <span className="text-xs">{busyId === candidate.candidate_id ? 'saving…' : 'use'}</span>
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

/** A crop from the uploaded PDF, as it is shown everywhere on this screen: whole, on white. */
const CROP_CLASS = 'max-h-40 w-fit max-w-full rounded-md border bg-white object-contain';

function MeasureCandidateCrop({ candidate, packageId }: { candidate: CandidateOut; packageId: string }) {
  const [state, setState] = useState<{ url: string } | { error: string } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [inView, setInView] = useState(false);
  const placeholderRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    const placeholder = placeholderRef.current;
    if (!placeholder) return;
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setInView(true);
          observer.disconnect();
        }
      },
      { rootMargin: '200px' },
    );
    observer.observe(placeholder);
    return () => observer.disconnect();
  }, []);

  const shouldLoad = inView || typeof IntersectionObserver === 'undefined';

  useEffect(() => {
    if (!shouldLoad) return;
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
  }, [candidate.candidate_id, packageId, shouldLoad, attempt]);

  if (state && 'error' in state) {
    return (
      <LoadError role="status" onRetry={() => { setState(null); setAttempt((count) => count + 1); }} retryLabel="Retry crop">
        {state.error}
      </LoadError>
    );
  }
  if (!state || !('url' in state)) {
    return (
      <span ref={placeholderRef} className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {shouldLoad ? <><ScanLine className="size-3.5" aria-hidden="true" /> Loading crop…</> : 'Scroll this reading into view to load its crop.'}
      </span>
    );
  }
  const warning = candidateCropWarning(candidate.crop_shows_gv_mark);
  return (
    <figure className="flex flex-col gap-1">
      <img
        className={CROP_CLASS}
        src={state.url}
        alt={`Mechanical crop for ${candidate.raw_text} on page ${candidate.page_index + 1}`}
      />
      {warning && <Caution>{warning}</Caution>}
    </figure>
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
    return <Hint>{state.error}</Hint>;
  }
  if (!state || !('url' in state)) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <ScanLine className="size-3.5" aria-hidden="true" /> Loading crop…
      </span>
    );
  }
  return (
    <figure className="flex flex-col gap-1">
      <img
        className={CROP_CLASS}
        src={state.url}
        alt={`Plan-view crop for ${discriminatorName}: ${proposal.value}`}
      />
      <figcaption className="text-xs text-muted-foreground">Evidence picture for this proposed layout</figcaption>
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
  const [state, setState] = useState<{ url: string } | { error: string } | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    let objectUrl: string | null = null;
    void downloadSettingPassageCrop(projectId(), packageId, pointer.proposal_id).then(
      (blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (live) setState({ url: objectUrl });
        else URL.revokeObjectURL(objectUrl);
      },
      () => {
        if (live) setState({ error: 'The picture of this passage could not be loaded.' });
      },
    );
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [packageId, pointer.proposal_id, attempt]);

  const page = pointer.page_index + 1;
  if (state && 'error' in state) {
    return (
      <LoadError role="status" onRetry={() => { setState(null); setAttempt((count) => count + 1); }} retryLabel="Retry passage picture">
        {state.error} Open page {page} of the {uploadLabel(pointer.document_kind)} and read it there.
      </LoadError>
    );
  }
  if (!state || !('url' in state)) {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <ScanLine className="size-3.5" aria-hidden="true" /> Loading the passage…
      </span>
    );
  }
  return (
    <figure className="flex flex-col gap-1">
      <img
        className={CROP_CLASS}
        src={state.url}
        alt={`The passage on page ${page} where the architect's drawing states ${name}`}
      />
      <figcaption className="text-xs text-muted-foreground">Page {page} of the {uploadLabel(pointer.document_kind)}</figcaption>
    </figure>
  );
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
    <div className="flex flex-col gap-2">
      {sources.length === 1 ? (
        <Hint>
          Source: <span className="font-medium text-foreground">{sources[0].value}</span>
        </Hint>
      ) : (
        <label className="flex flex-col gap-1 text-xs text-muted-foreground" htmlFor={`p-${parameter.name}-source`}>
          Where from?
          <select
            className={SELECT_CLASS}
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
      {selected && <Hint>{selected.guidance}</Hint>}
      <input
        className={INPUT_CLASS}
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

/** The step's own heading, for screen readers and for focus after Next or Back (#1124). */
function StepHeading({ step }: { step: MeasureStep }) {
  const index = MEASURE_STEPS.findIndex((item) => item.id === step);
  return (
    <h3 tabIndex={-1} data-step-heading={step} className="sr-only">
      Step {index + 1} of {MEASURE_STEPS.length}: {MEASURE_STEPS[index].label}
    </h3>
  );
}
