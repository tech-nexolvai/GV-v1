import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MessageSquare, MoreHorizontal, SlidersHorizontal } from 'lucide-react';

import { getPackage, ApiError, downloadPagePicture } from '@/api/client';
import { projectId as configuredProjectId } from '@/api/config';
import { liveDraftCloudApi, makeDraftCloud, type DraftCloudApi } from '@/api/draft-cloud';
import { liveFinalCloudApi, type FinalCloudApi } from '@/api/final-cloud';
import { aiReviewOf, apiUrl, architectCompareCountsOf, architectCompareOf, liveReviewItemsApi, NOT_CHECKED_GROUP, NOT_COMPARED_GROUP, PAGES_GROUP, type ActionExtra, type AiReviewRow, type ArchitectCompareEntry, type ItemAction, type JobValueSuggestion, type ReviewFailure, type ReviewItem, type ReviewItemsApi, type ReviewItemsOut } from '@/api/review-items';
import { agreeAction, disagreeAction, leftWords, openNoCountertopPages, openPagesToDecide, orderItems, proposedWall, summarize, whereWord } from '@/lib/review-items';
import type { AssistantRecords } from '@/lib/assistant';
import { AssistantPanel, type AssistantNavigation } from '@/components/assistant/assistant-panel';
import { HeaderActions } from '@/components/shell/ShellHeader';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { ReviewHeader, type CloudState } from '@/components/simple-review/review-header';
import { FailuresStrip, type SaveError } from '@/components/simple-review/failures-strip';
import { ItemRow } from '@/components/simple-review/item-row';
import { ItemDetail } from '@/components/simple-review/item-detail';
import { NotCheckedGroup, type JobValue } from '@/components/simple-review/not-checked-group';
import { FinalBar } from '@/components/simple-review/final-bar';
import { LoadError, NothingNeedsYou, ReviewSkeleton, UnaccountedBanner } from '@/components/simple-review/states';
import { PicturesProvider, type PictureLoader } from '@/components/simple-review/item-pictures';
import { PagesChoice } from '@/components/simple-review/pages-choice';
import { ArchitectProvider } from '@/components/simple-review/architect-choices';
import { liveArchitectLoader, type ArchitectLoader } from '@/components/simple-review/architect-choices-lib';
import { RecordedNumbersPanel, type RecordedPanelSubject } from '@/components/simple-review/recorded-numbers';
import { liveRecordedNumbers, type RecordedNumbersLoader } from '@/components/simple-review/recorded-numbers-lib';
import type { ViewerItem } from '@/components/simple-review/dual-pdf-viewer';
import { ArchitectCompare } from '@/components/simple-review/architect-compare';
import { AiReview } from '@/components/simple-review/ai-review';
import { aiReviewEntries, aiReviewWhat } from '@/components/simple-review/ai-review-lib';
import { compareWhat, notComparedKeys } from '@/components/simple-review/architect-compare-lib';

// "Open PDFs" opens both full drawings side by side (U3, #1355), loaded only when first opened.
const DualPdfViewer = lazy(() => import('@/components/simple-review/dual-pdf-viewer').then((m) => ({ default: m.DualPdfViewer })));

export interface SimpleReviewPageProps {
  packageId: string;
  /** The read and write calls (W1 + W3). Tests pass their own; the app uses the live ones. */
  api?: ReviewItemsApi;
  /** How a stored picture becomes an image. Tests and screenshots pass their own. */
  pictures?: PictureLoader;
  /** The draft cloud PDF (W4b, #1340): ask, wait, download (#1394). Tests pass their own. */
  cloud?: DraftCloudApi;
  /** How often a draft being made is asked about again. Tests pass 0. */
  draftPollMs?: number;
  /** Sign-off and the final cloud PDF (#1368). Tests pass their own. */
  finalCloud?: FinalCloudApi;
  /** The architect view match and pairing reads behind an architect question (#1368). */
  architect?: ArchitectLoader;
  /**
   * "Open PDFs" (O): `item` is the spot to open both documents at (a list item or a wrong result),
   * null for the whole set. Without it the side-by-side viewer (U3, #1355) opens.
   */
  onOpenPdfs?: (item: ViewerItem | null) => void;
  /** What a wrong result's check recorded, shown beside the drawings when opened from it (#1388). */
  recorded?: RecordedNumbersLoader;
  /** The old tabs and wizard, `#/review/<id>/advanced`. */
  onOpenAdvanced: () => void;
  onTitleChange?: (title: string, revision: number | null) => void;
  /** The page's own "items need you" count (`header.open`), so the sidebar shows the same number. */
  onNeedYouChange?: (count: number | null) => void;
  /** The review assistant, docked on the right. Off in tests that do not exercise it. */
  assistantAvailable?: boolean;
}

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; error: Error }
  | { status: 'ready'; data: ReviewItemsOut };

/** The live picture loader: stored page pictures and the close-ups W1 points at. */
function useLivePictures(project: string | null, packageId: string): PictureLoader {
  const urls = useRef<string[]>([]);
  useEffect(() => () => {
    for (const url of urls.current) URL.revokeObjectURL(url);
    urls.current = [];
  }, []);
  return useCallback(async (source) => {
    if (project === null) throw new Error('No project is configured.');
    let blob: Blob;
    if (source.kind === 'page') {
      blob = await downloadPagePicture(project, packageId, source.page, source.documentVersionId);
    } else {
      const response = await fetch(apiUrl(source.path), { headers: { Accept: 'image/png,image/*;q=0.8' } });
      if (!response.ok) throw new Error(`The server answered ${response.status}.`);
      blob = await response.blob();
    }
    const url = URL.createObjectURL(blob);
    urls.current.push(url);
    return url;
  }, [project, packageId]);
}

/** A failed save in words. 501 is W3's "not built yet"; 404 and 409 keep their own meaning. */
/** The item kinds about one countertop's width check: their recorded numbers sit beside the drawings (#1399). */
const COUNTERTOP_KINDS: ReadonlySet<ReviewItem['kind']> = new Set(['countertop_by_hand', 'wall_choice']);

/** A countertop item, as the "What the check recorded" panel's subject; null for any other item. */
function countertopSubject(item: ReviewItem | null): RecordedPanelSubject | null {
  if (!item || !COUNTERTOP_KINDS.has(item.kind) || (!item.finding_id && !item.row_id)) return null;
  return { key: item.key, finding_id: item.finding_id, row_id: item.row_id, why: item.why, page: item.page, countertop: item.countertop, row_label: item.row_label, tone: 'review' };
}

function errorWords(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 501) return `This answer cannot be saved yet: ${error.message}`;
    if (error.status === 404) return `Not found: ${error.message}`;
    if (error.status === 409) return `This item changed since you opened it, so the list was reloaded. Look again, then Retry. (${error.message})`;
  }
  return error instanceof Error ? error.message : String(error);
}

function reducedMotion(): boolean {
  return typeof window === 'undefined' || typeof window.matchMedia !== 'function' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return true;
  return target.closest('[role="menu"], [role="dialog"], [role="listbox"]') !== null;
}

/** Where the "Not checked" group's busy state and save error are kept (not an API key). */
const NOT_CHECKED = 'group:not-checked';
/** Where the "no countertop" pages group's busy state and save error are kept. */
const PAGES = 'group:no-countertop-pages';
/** Where the "Clear all as not compared" group's busy state and save error are kept. */
const NOT_COMPARED = 'group:not-compared';

/** What a saved answer is announced as (role=status), in a few plain words. */
const SAVED_WORDS: Partial<Record<ItemAction, string>> = {
  confirm: 'Confirmed. The checks run again with this reading.',
  fix: "Fixed. Recorded as a correction of the AI's reading; the checks run again.",
  checked_right: 'Saved: checked by hand, right.',
  checked_wrong: 'Saved: checked by hand, wrong. It will be clouded.',
  choose_wall: 'Walls saved. The width check runs again.',
  confirm_no_countertop: 'Saved: no countertop on these pages.',
  there_is_a_countertop: 'Saved: there is a countertop. It is checked in the next run.',
  answer: "Saved. The checks run again with the architect's answer.",
  not_this: 'Saved.',
  agree: 'Agreed. It will be clouded as wrong.',
  settle_group: 'Cleared as not checked. They are printed NOT CHECKED.',
  job_values: "Saved on this job. The checks run again with this job's values.",
  clear_not_compared: 'Cleared as not compared.',
};

const NO_RECORDS: AssistantRecords = {
  rows: [],
  rowsReady: false,
  pagesWithoutCountertop: [],
  rowsNotChecked: [],
  findings: [],
  blocking: null,
};

/**
 * The simple review page (#1343): one page with only what needs a person. Items are closed by
 * default; opening one shows the vendor page with the spot ringed, the architect page and a close-up,
 * with one-click answers. Results to be clouded as wrong sit above, the checks that did not run sit
 * quietly below, and the final cloud PDF unlocks when nothing is open. Nothing here asks to type.
 */
export function SimpleReviewPage({
  packageId,
  api = liveReviewItemsApi,
  pictures,
  cloud = liveDraftCloudApi,
  draftPollMs,
  finalCloud = liveFinalCloudApi,
  architect = liveArchitectLoader,
  recorded = liveRecordedNumbers,
  onOpenPdfs,
  onOpenAdvanced,
  onTitleChange,
  onNeedYouChange,
  assistantAvailable = true,
}: SimpleReviewPageProps) {
  const project = useMemo(() => {
    try {
      return configuredProjectId();
    } catch {
      return null;
    }
  }, []);
  const livePictures = useLivePictures(project, packageId);

  const [version, setVersion] = useState(0);
  const [load, setLoad] = useState<{ version: number; state: LoadState } | null>(null);
  // The last good answer stays on screen while a reload is in flight, so the list never flashes.
  const [lastGood, setLastGood] = useState<ReviewItemsOut | null>(null);
  const [title, setTitle] = useState<{ vendor: string; revision: number | null; approved: boolean } | null>(null);

  const [openKey, setOpenKey] = useState<string | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [fixOpenKey, setFixOpenKey] = useState<string | null>(null);
  const [busy, setBusy] = useState<{ key: string; action: ItemAction; extra?: ActionExtra } | null>(null);
  // One save at a time, checked synchronously: a second click before the re-render is ignored.
  const saving = useRef(false);
  const [errors, setErrors] = useState<Record<string, SaveError>>({});
  const [draft, setDraft] = useState<CloudState>({ status: 'idle' });
  const [saved, setSaved] = useState<{ words: string; at: number } | null>(null);
  const [notCheckedOpen, setNotCheckedOpen] = useState(false);
  // An entry the Assistant asked to open, focused once it is on screen (#1368).
  const pendingFocus = useRef<string | null>(null);
  const [focusAsk, setFocusAsk] = useState(0);
  // The architect proposal each loaded architect question offers, for the C key, with the result it
  // was read for: a proposal read for an older result is never sent (#1368).
  const proposals = useRef(new Map<string, { findingId: string | null; extra: ActionExtra | null }>());
  // Bumped to read the open architect question again (after a stale answer, #1368).
  const [architectAsk, setArchitectAsk] = useState(0);
  // Opened from a wrong row (#1388) or a countertop item (#1399), `recorded` is what it is about: the
  // numbers its check recorded sit beside the drawings.
  const [viewer, setViewer] = useState<{ opening: number; item: ViewerItem | null; recorded: RecordedPanelSubject | null } | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(() => assistantAvailable && typeof window !== 'undefined' && window.matchMedia('(min-width: 1440px)').matches);
  const [assistantMounted, setAssistantMounted] = useState(assistantOpen);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    if (project === null) return;
    let current = true;
    api.load(project, packageId).then(
      (data) => {
        if (!current) return;
        setLoad({ version, state: { status: 'ready', data } });
        setLastGood(data);
      },
      (error: unknown) => current && setLoad({ version, state: { status: 'error', error: error instanceof Error ? error : new Error(String(error)) } }),
    );
    return () => {
      current = false;
    };
  }, [api, project, packageId, version]);

  // The sidebar shows the same "items need you" count as this page (its list's `header.open`).
  const openCount = lastGood?.header.open ?? null;
  useEffect(() => {
    if (openCount !== null) onNeedYouChange?.(openCount);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openCount]);

  useEffect(() => {
    if (project === null) return;
    let current = true;
    getPackage(project, packageId).then(
      (detail) => {
        if (!current) return;
        const vendor = detail.vendor ?? 'Untitled document set';
        const revision = detail.current_revision_number ?? null;
        setTitle({ vendor, revision, approved: detail.state === 'APPROVED' });
        onTitleChange?.(vendor, revision);
      },
      () => undefined, // The items still show; the header falls back to a plain name.
    );
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project, packageId]);

  const state: LoadState =
    project === null
      ? { status: 'error', error: new Error('VITE_PROJECT_ID is not set, so this page does not know which project to ask about.') }
      : load === null
        ? { status: 'loading' }
        : load.version === version || load.state.status === 'error'
          ? load.state
          : lastGood
            ? { status: 'ready', data: lastGood }
            : { status: 'loading' };
  const data = state.status === 'ready' ? state.data : null;
  const summary = useMemo(() => (data ? summarize(data) : null), [data]);
  // The open "no countertop?" pages are one choice card of their own; the list holds the rest.
  const pagesAsked = useMemo(() => (data ? openNoCountertopPages(data.items) : []), [data]);
  // Older or doubtful readings: asked one page at a time, nothing proposed (#1394).
  const pagesToDecide = useMemo(() => (data ? openPagesToDecide(data.items) : []), [data]);
  const ordered = useMemo(
    () => (data ? orderItems(data.items).filter((item) => !(item.kind === 'no_countertop_page' && item.status === 'open')) : []),
    [data],
  );
  const reload = useCallback(() => setVersion((n) => n + 1), []);

  // The newest list, for Retry: after a 409 the list is reloaded first, and Retry then sends the
  // finding the reviewer now sees, never the stale one that was refused.
  const latest = useRef<ReviewItemsOut | null>(null);
  useEffect(() => {
    latest.current = data;
    if (!data) return;
    // An architect proposal is kept only while its item is open on the same result; anything else
    // (a re-check, an answer, the item gone) drops it, and the C key then opens the item to read it again.
    for (const [key, kept] of proposals.current) {
      const now = data.items.find((entry) => entry.key === key);
      if (!now || now.status !== 'open' || (now.finding_id ?? null) !== kept.findingId) proposals.current.delete(key);
    }
  }, [data]);

  function findingNow(key: string, fallback: string | null): string | null {
    const now = latest.current;
    if (!now) return fallback;
    const entry = [...now.items, ...now.failures, ...now.not_checked].find((candidate) => candidate.key === key);
    return entry ? (entry.finding_id ?? null) : fallback;
  }

  async function send(key: string, action: ItemAction, findingId: string | null, extra?: ActionExtra) {
    if (project === null) return;
    await api.act(project, packageId, key, action, { expected_finding_id: findingId, ...extra });
  }

  function failed(errorKey: string, error: unknown, retry: () => void) {
    // A stale item: reload the list now, so what the reviewer looks at before Retry is current.
    if (error instanceof ApiError && error.status === 409) reload();
    setErrors((all) => ({ ...all, [errorKey]: { message: errorWords(error), retry } }));
  }

  function cleared(errorKey: string) {
    setErrors((all) => {
      const rest = { ...all };
      delete rest[errorKey];
      return rest;
    });
  }

  function announce(action: ItemAction) {
    const words = SAVED_WORDS[action];
    // Counted, so the same words saved twice are a new announcement (the notice re-renders its line).
    if (words) setSaved((now) => ({ words, at: (now?.at ?? 0) + 1 }));
  }

  /** Starts a save, or says no when one is already being saved. */
  function begin(next: { key: string; action: ItemAction; extra?: ActionExtra }): boolean {
    if (saving.current) return false;
    saving.current = true;
    setBusy(next);
    return true;
  }

  function finish() {
    saving.current = false;
    setBusy(null);
  }

  async function act(key: string, action: ItemAction, findingId: string | null, extra?: ActionExtra) {
    if (!begin({ key, action, extra })) return;
    // The Fix dialog stays open until the correction is saved, so a refusal is read where it was typed.
    if (action !== 'fix') setFixOpenKey(null);
    const architect = extra?.architect_view !== undefined || extra?.architect_pairing !== undefined;
    try {
      await send(key, action, findingId, extra);
      cleared(key);
      setFixOpenKey(null);
      announce(action === 'answer' && !architect ? 'not_this' : action);
      reload();
    } catch (error) {
      if (architect && error instanceof ApiError && error.status === 409) {
        // The architect answer names the record it was shown, which moved on: Retry reads the
        // question again, and the reviewer answers what is there now. The refused answer is never resent.
        proposals.current.delete(key);
        failed(key, error, () => {
          cleared(key);
          setArchitectAsk((n) => n + 1);
        });
      } else {
        failed(key, error, () => void act(key, action, findingNow(key, findingId), retried(key, action, extra)));
      }
    } finally {
      finish();
    }
  }

  // A Confirm on a number sends the value it showed: after a 409 reload, Retry sends the value the
  // reviewer now sees, never the stale one (#1358).
  function retried(key: string, action: ItemAction, extra?: ActionExtra): ActionExtra | undefined {
    if (action !== 'confirm' || extra?.shown_value === undefined) return extra;
    const now = latest.current?.items.find((entry) => entry.key === key);
    return now?.best_guess ? { ...extra, shown_value: now.best_guess.text } : extra;
  }

  function actOnItem(item: ReviewItem, action: ItemAction, extra?: ActionExtra) {
    if (saving.current) return;
    let body = extra;
    if (action === 'confirm' && (item.kind === 'number' || item.kind === 'depth_reading') && item.best_guess) {
      // The value the reviewer saw: W2 records exactly the stored value, and refuses another (#1358).
      // A depth or sink reading's Confirm is the same W2 confirm (#1360).
      body = { ...extra, shown_value: item.best_guess.text };
    }
    if (action === 'answer' && item.kind === 'architect' && !body?.architect_view && !body?.architect_pairing) {
      // The C key on an architect question picks its proposal, once its choices have loaded.
      const kept = proposals.current.get(item.key);
      const proposal = kept && kept.findingId === (item.finding_id ?? null) && item.status === 'open' ? kept.extra : null;
      if (proposal === null) {
        setOpenKey(item.key);
        return;
      }
      body = { ...body, ...proposal };
    }
    if (action === 'choose_wall' && !body?.wall_config) {
      // The C key on a wall choice picks the readers' proposal; with none, the reviewer clicks one.
      const proposal = proposedWall(item);
      if (proposal === null) {
        setOpenKey(item.key);
        return;
      }
      body = { ...body, wall_config: proposal };
    }
    void act(item.key, action, item.finding_id ?? null, body);
  }
  const agree = (failure: ReviewFailure) => void act(failure.key, 'agree', failure.finding_id);

  // "Clear all as not checked": one group call naming exactly the results the reviewer sees
  // (`expected_keys`, #1358). W3 records each in one transaction, or nothing when the group changed
  // (409: the list reloads, and Retry sends the group as it is shown then).
  async function clearNotChecked() {
    const keys = (latest.current?.not_checked ?? []).filter((row) => row.status === 'open').map((row) => row.key);
    if (!begin({ key: NOT_CHECKED, action: 'settle_group' })) return;
    try {
      await send(NOT_CHECKED_GROUP, 'settle_group', null, { expected_keys: keys });
      cleared(NOT_CHECKED);
      announce('settle_group');
      reload();
    } catch (error) {
      failed(NOT_CHECKED, error, () => void clearNotChecked());
    } finally {
      finish();
    }
  }

  // "Enter this job's values" (#1403): saved on the project, then the checks run again (coalesced).
  async function saveJobValues(values: JobValue[]) {
    if (!begin({ key: NOT_CHECKED, action: 'job_values' })) return;
    try {
      await send(NOT_CHECKED_GROUP, 'job_values', null, { job_values: values });
      cleared(NOT_CHECKED);
      announce('job_values');
      reload();
    } catch (error) {
      failed(NOT_CHECKED, error, () => void saveJobValues(values));
    } finally {
      finish();
    }
  }

  // "Clear all as not compared": one group call naming exactly the architect numbers shown with no
  // match or not read (`expected_keys`); a 409 reloads, and Retry sends the group as shown then.
  async function clearNotCompared() {
    const keys = latest.current ? notComparedKeys(architectCompareOf(latest.current)) : [];
    if (!begin({ key: NOT_COMPARED, action: 'clear_not_compared' })) return;
    try {
      await send(NOT_COMPARED_GROUP, 'clear_not_compared', null, { expected_keys: keys });
      cleared(NOT_COMPARED);
      announce('clear_not_compared');
      reload();
    } catch (error) {
      failed(NOT_COMPARED, error, () => void clearNotCompared());
    } finally {
      finish();
    }
  }

  // "These N pages have no countertop": one group call naming exactly the pages shown (#1358).
  async function confirmPages() {
    const keys = openNoCountertopPages(latest.current?.items ?? []).map((item) => item.key);
    if (!begin({ key: PAGES, action: 'confirm_no_countertop' })) return;
    try {
      await send(PAGES_GROUP, 'confirm_no_countertop', null, { expected_keys: keys });
      cleared(PAGES);
      announce('confirm_no_countertop');
      reload();
    } catch (error) {
      failed(PAGES, error, () => void confirmPages());
    } finally {
      finish();
    }
  }

  const openPdfs = useCallback((item: ReviewItem | null) => {
    if (onOpenPdfs) onOpenPdfs(item);
    else setViewer((now) => ({ opening: (now?.opening ?? 0) + 1, item, recorded: countertopSubject(item) }));
  }, [onOpenPdfs]);

  // A wrong row's Look (or O once it is selected, #1388): both drawings at its page and spot.
  function lookAtFailure(failure: ReviewFailure) {
    setActiveKey(failure.key);
    const at: ViewerItem = { key: failure.key, page: failure.page ?? null, what: whereWord(failure), status: failure.status, vendor_ref: failure.vendor_ref ?? null, architect_ref: null };
    if (onOpenPdfs) onOpenPdfs(at);
    else setViewer((now) => ({ opening: (now?.opening ?? 0) + 1, item: at, recorded: { ...failure, tone: 'fail' } }));
  }

  // An architect comparison's Look: both drawings, the vendor's and the architect's numbers ringed.
  function lookAtCompare(entry: ArchitectCompareEntry) {
    const spot = (ref: ArchitectCompareEntry['vendor_ref']) => (ref ? { ...ref, page_id: '', coordinate_space: 'stored' } : null);
    const location = spot(entry.architect_ref);
    const at: ViewerItem = {
      key: entry.key,
      page: entry.vendor_ref?.page_number ?? entry.page,
      what: compareWhat(entry),
      status: entry.result === 'same' ? 'done' : 'open',
      vendor_ref: spot(entry.vendor_ref),
      architect_ref: { location, reason: location ? null : "No spot on the architect's drawing was recorded for this number." },
    };
    if (onOpenPdfs) onOpenPdfs(at);
    else setViewer((now) => ({ opening: (now?.opening ?? 0) + 1, item: at, recorded: null }));
  }

  // An AI page review row's Look: both drawings at its page, the vendor's number ringed when recorded.
  function lookAtAiRow(row: AiReviewRow, page: number) {
    const location = row.architect_ref ? { ...row.architect_ref, page_id: '', coordinate_space: 'stored' } : null;
    const at: ViewerItem = {
      key: row.key,
      page: row.vendor_box?.page_number ?? page,
      what: aiReviewWhat(row),
      status: row.status,
      vendor_ref: row.vendor_box ? { ...row.vendor_box, page_id: '', coordinate_space: 'stored' } : null,
      architect_ref: { location, reason: location ? null : "No spot on the architect's drawing was recorded for this number." },
    };
    if (onOpenPdfs) onOpenPdfs(at);
    else setViewer((now) => ({ opening: (now?.opening ?? 0) + 1, item: at, recorded: null }));
  }

  // A job value's Look (#1409): the side-by-side viewer at the page the reader found it on, ringed.
  function lookAtJobValue(found: JobValueSuggestion, name: string) {
    const at: ViewerItem = { key: `job-value:${found.name}`, page: found.page, what: `${name} ${found.value}`, status: 'open', vendor_ref: null, architect_ref: { location: found.location ?? null, reason: found.location ? null : 'The reader recorded no spot for this value.' } };
    if (onOpenPdfs) onOpenPdfs(at);
    else setViewer((now) => ({ opening: (now?.opening ?? 0) + 1, item: at, recorded: null }));
  }

  // The newest draft ask: an older one (or one from a closed page) stops waiting and saves nothing.
  const draftAsk = useRef(0);
  useEffect(() => () => {
    draftAsk.current += 1;
  }, []);

  /** Ask for the draft cloud PDF, wait while the worker makes it, then save it (#1394). */
  async function runDraft() {
    if (project === null || draft.status === 'working') return;
    const ask = ++draftAsk.current;
    setDraft({ status: 'working' });
    try {
      const blob = await makeDraftCloud(cloud, project, packageId, { pollMs: draftPollMs, stillWanted: () => ask === draftAsk.current });
      if (blob === null || ask !== draftAsk.current) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `gv-review-${packageId}-draft-cloud.pdf`;
      link.click();
      URL.revokeObjectURL(url);
      setDraft({ status: 'ready' });
    } catch (error) {
      // The server's own reason ("Run the checks first."), never the item-save wording.
      if (ask === draftAsk.current) setDraft({ status: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  }

  function toggle(key: string) {
    setActiveKey(key);
    setFixOpenKey(null);
    setOpenKey((now) => (now === key ? null : key));
  }

  function focusRow(key: string) {
    setActiveKey(key);
    const row = rowRefs.current.get(key);
    row?.focus({ preventScroll: true });
    // Kept clear of the sticky final bar (the row's scroll margin), smoothly unless motion is reduced.
    row?.scrollIntoView?.({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
  }

  // J/K move, Enter opens, C agrees, F disagrees (Fix opens its menu), O opens the PDFs, Esc closes.
  useEffect(() => {
    if (!data) return;
    function onKey(event: KeyboardEvent) {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
      const keys = ordered.map((item) => item.key);
      const key = event.key.toLowerCase();
      const targetKey = openKey ?? activeKey;
      const item = ordered.find((entry) => entry.key === targetKey) ?? null;
      // A wrong row the reviewer clicked (#1388): O opens it, the other keys stay with the list.
      const failure = data?.failures.find((entry) => entry.key === activeKey) ?? null;
      if (key === 'j' || key === 'k') {
        if (keys.length === 0) return;
        event.preventDefault();
        const at = activeKey === null ? -1 : keys.indexOf(activeKey);
        const next = key === 'j' ? Math.min(at + 1, keys.length - 1) : Math.max(at - 1, 0);
        focusRow(keys[next]);
      } else if (event.key === 'Enter') {
        // A focused row is a button and opens itself; this covers the list with nothing focused.
        if (event.target instanceof HTMLButtonElement || event.target instanceof HTMLAnchorElement || activeKey === null || failure) return;
        event.preventDefault();
        toggle(activeKey);
      } else if (event.key === 'Escape') {
        if (openKey === null) return;
        event.preventDefault();
        const closed = openKey;
        setOpenKey(null);
        setFixOpenKey(null);
        focusRow(closed);
      } else if (key === 'c' && item) {
        const action = agreeAction(item);
        if (!action || busy) return;
        event.preventDefault();
        actOnItem(item, action);
      } else if (key === 'f' && item) {
        const action = disagreeAction(item);
        if (!action || busy) return;
        event.preventDefault();
        if (action === 'fix') {
          setOpenKey(item.key);
          setFixOpenKey(item.key);
        } else {
          actOnItem(item, action);
        }
      } else if (key === 'o') {
        event.preventDefault();
        if (failure) lookAtFailure(failure);
        else openPdfs(item);
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  });

  const assistantNav: AssistantNavigation = {
    openPage: (page) => openPdfs(ordered.find((item) => item.page === page) ?? null),
    showRow: (row) => {
      const item = ordered.find((entry) => entry.row_id === row.row_id);
      if (item) toggle(item.key);
    },
    showFinding: (finding) => {
      const item = ordered.find((entry) => entry.finding_id === finding.id);
      if (item) toggle(item.key);
    },
    openQueue: (key) => {
      if (ordered.some((item) => item.key === key)) toggle(key);
    },
    openReviewItem: (key, page) => openEntry(key, page),
  };

  /**
   * The Assistant's "open item" (#1354): open the entry, scroll to it and focus it. A list item opens
   * its detail; a wrong result, a page with no countertop or a check that did not run is scrolled to
   * (its group opened first); an entry no longer listed opens the drawings at its page instead.
   */
  function openEntry(key: string, page: number | null) {
    const listed = latest.current;
    if (!listed) return;
    if (ordered.some((item) => item.key === key)) {
      setActiveKey(key);
      setFixOpenKey(null);
      setOpenKey(key);
      focusWhenShown(key);
      return;
    }
    if (listed.not_checked.some((entry) => entry.key === key)) setNotCheckedOpen(true);
    const aiListed = aiReviewOf(listed);
    const known = (aiListed !== null && aiReviewEntries(aiListed).some((entry) => entry.row.key === key)) || architectCompareOf(listed).some((entry) => entry.key === key) || listed.failures.some((entry) => entry.key === key) || listed.not_checked.some((entry) => entry.key === key) || pagesAsked.some((item) => item.key === key) || pagesToDecide.some((item) => item.key === key);
    if (known) {
      focusWhenShown(key);
      return;
    }
    if (page !== null) openPdfs(ordered.find((item) => item.page === page) ?? null);
  }

  function focusWhenShown(key: string) {
    pendingFocus.current = key;
    setFocusAsk((n) => n + 1);
  }

  // After the render that shows it (an opened item, an opened group), scroll to it and focus it.
  useEffect(() => {
    const key = pendingFocus.current;
    if (key === null) return;
    const target = rowRefs.current.get(key) ?? document.querySelector<HTMLElement>(`[data-entry-key="${CSS.escape(key)}"]`);
    if (!target) return;
    pendingFocus.current = null;
    target.scrollIntoView?.({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
    target.focus({ preventScroll: true });
  }, [focusAsk, openKey, notCheckedOpen, data]);
  const opened = ordered.find((item) => item.key === openKey) ?? null;

  function toggleAssistant() {
    setAssistantMounted(true);
    setAssistantOpen((now) => !now);
  }

  const name = title?.vendor ?? 'This drawing set';
  const needsYou = ordered.filter((item) => item.status !== 'done');
  const checked = ordered.filter((item) => item.status === 'done');

  function renderRows(items: readonly ReviewItem[]) {
    return (
      <ul className="divide-y divide-border">
        {items.map((item) => {
          const open = openKey === item.key;
          return (
            <li key={item.key}>
              <ItemRow
                ref={(node) => {
                  if (node) rowRefs.current.set(item.key, node);
                  else rowRefs.current.delete(item.key);
                }}
                item={item}
                open={open}
                active={activeKey === item.key}
                onToggle={() => toggle(item.key)}
                onFocus={() => setActiveKey(item.key)}
              />
              {open && (
                <div className="animate-in fade-in-0 slide-in-from-top-1 duration-150 motion-reduce:animate-none">
                  <ItemDetail
                    item={item}
                    busy={busy?.key === item.key ? busy.action : null}
                    sending={busy?.key === item.key ? busy.extra ?? null : null}
                    refresh={architectAsk}
                    error={errors[item.key] ?? null}
                    fixOpen={fixOpenKey === item.key}
                    onFixOpenChange={(next) => setFixOpenKey(next ? item.key : null)}
                    onAct={(action, extra) => actOnItem(item, action, extra)}
                    onOpenPdfs={openPdfs}
                    onOpenAdvanced={onOpenAdvanced}
                    onProposal={(extra) => proposals.current.set(item.key, { findingId: item.finding_id ?? null, extra })}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <PicturesProvider load={pictures ?? livePictures}>
      <ArchitectProvider projectId={project} packageId={packageId} loader={architect}>
      <div data-tw data-slot="simple-review" className="@container/review flex h-full min-h-0 w-full min-w-0 font-sans text-foreground antialiased">
        <HeaderActions>
          {assistantAvailable && (
            <Button
              variant={assistantOpen ? 'secondary' : 'ghost'}
              size="sm"
              onClick={toggleAssistant}
              aria-label="Assistant"
              aria-expanded={assistantOpen}
              aria-controls={assistantMounted ? 'review-assistant' : undefined}
            >
              <MessageSquare /> <span className="hidden xl:inline">Assistant</span>
            </Button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon-sm" aria-label="More">
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={onOpenAdvanced}>
                <SlidersHorizontal aria-hidden="true" />
                Advanced view (admin)
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </HeaderActions>

        <div className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex min-h-full w-full max-w-[60rem] flex-col px-4 sm:px-6">
            {state.status === 'loading' && <ReviewSkeleton />}
            {state.status === 'error' && !data && <LoadError error={state.error} onRetry={reload} />}
            {data && summary && (
              <>
                <ReviewHeader
                  title={name}
                  revision={title?.revision ?? data.revision_number}
                  header={data.header}
                  onOpenPdfs={() => openPdfs(opened)}
                  onDraftCloud={project !== null ? () => void runDraft() : null}
                  draft={draft}
                />
                {data.unaccounted.length > 0 && <UnaccountedBanner count={data.unaccounted.length} onOpenAdvanced={onOpenAdvanced} />}

                <p role="status" aria-live="polite" data-slot="saved-notice" className="sr-only">
                  {/* A new line per save, so the same words saved again are announced again. */}
                  {saved && <span key={saved.at}>{saved.words}</span>}
                </p>

                <div className="flex flex-col gap-10 pt-2">
                  {/* First: the AI page review, every architect number beside the vendor's. Hidden when absent. */}
                  {/* No person answers its rows: a different row is clouded on the PDF by itself (decision 2026-10-16). */}
                  <AiReview review={aiReviewOf(data)} onLook={lookAtAiRow} />

                  {/* The old direct compare, only for a package with no AI page review (#1421). */}
                  {aiReviewOf(data) === null && <ArchitectCompare
                    entries={architectCompareOf(data)}
                    counts={architectCompareCountsOf(data)}
                    onLook={lookAtCompare}
                    busyKey={busy?.key ?? null}
                    errors={errors}
                    // An architect comparison names no finding: the key is what W3 checks.
                    onAct={(entry, action) => void act(entry.key, action, null)}
                    groupKey={NOT_COMPARED}
                    onClearNotCompared={() => void clearNotCompared()}
                  />}

                  <FailuresStrip
                    failures={data.failures}
                    busyKey={busy?.key ?? null}
                    errors={errors}
                    selectedKey={activeKey}
                    onAgree={agree}
                    onLook={lookAtFailure}
                    onSelect={(failure) => setActiveKey(failure.key)}
                  />

                  {summary.nothingNeedsYou && pagesAsked.length === 0 && pagesToDecide.length === 0 && <NothingNeedsYou checked={checked.length} signedOff={title?.approved ?? false} />}

                  <PagesChoice
                    pages={pagesAsked}
                    toDecide={pagesToDecide}
                    onNoCountertop={(page) => actOnItem(page, 'confirm_no_countertop')}
                    busyKey={busy?.key ?? null}
                    groupKey={PAGES}
                    errors={errors}
                    onConfirmAll={() => void confirmPages()}
                    onThereIsOne={(page) => actOnItem(page, 'there_is_a_countertop')}
                    onLook={(page) => openPdfs(page)}
                  />

                  {needsYou.length > 0 && (
                    <section aria-labelledby="needs-you-title" className="flex flex-col gap-3">
                      <div className="flex items-baseline justify-between gap-4">
                        <h2 id="needs-you-title" className="text-[20px] leading-7 font-semibold tracking-[-0.01em]">
                          Needs you <span className="num ml-1 text-[15px] font-normal text-muted-foreground">{needsYou.filter((item) => item.status === 'open').length}</span>
                        </h2>
                        <p className="hidden text-[13px] text-muted-foreground md:block">
                          <span className="num">J</span>/<span className="num">K</span> to move, <span className="num">Enter</span> to open
                        </p>
                      </div>
                      {summary.rechecking.length > 0 && (
                        <p role="status" className="text-[13px] text-muted-foreground">
                          Re-checking {summary.rechecking.length} {summary.rechecking.length === 1 ? 'item' : 'items'} with your answers…
                        </p>
                      )}
                      <div className="overflow-hidden rounded-xl border border-border">{renderRows(needsYou)}</div>
                    </section>
                  )}

                  <NotCheckedGroup
                    open={notCheckedOpen}
                    onOpenChange={setNotCheckedOpen}
                    items={data.not_checked}
                    note={data.not_checked_note}
                    busy={busy?.key === NOT_CHECKED}
                    error={errors[NOT_CHECKED] ?? null}
                    onClearAll={() => void clearNotChecked()}
                    canSaveJobValues={data.can_save_job_values ?? false}
                    onSaveJobValues={(values) => void saveJobValues(values)}
                    suggestions={data.job_value_suggestions ?? []}
                    onLook={lookAtJobValue}
                  />

                  {checked.length > 0 && (
                    <section aria-labelledby="checked-title" className="flex flex-col gap-3">
                      <h2 id="checked-title" className="text-[15px] font-medium text-muted-foreground">
                        Checked <span className="num ml-1 font-normal">{checked.length}</span>
                      </h2>
                      <div className="overflow-hidden rounded-xl border border-border">{renderRows(checked)}</div>
                    </section>
                  )}
                </div>

                <div className="flex-1" />
                <FinalBar
                  summary={summary}
                  left={leftWords(data.header)}
                  api={finalCloud}
                  projectId={project}
                  packageId={packageId}
                  revision={title?.revision ?? data.revision_number}
                  approved={title?.approved ?? false}
                  onSigned={reload}
                />
              </>
            )}
          </div>
        </div>

        {assistantAvailable && assistantMounted && project !== null && (
          <AssistantPanel
            projectId={project}
            packageId={packageId}
            setName={name}
            open={assistantOpen}
            records={NO_RECORDS}
            context={opened && opened.page != null ? { page_number: opened.page, record_id: opened.finding_id ?? null } : null}
            nav={assistantNav}
            onClose={() => setAssistantOpen(false)}
          />
        )}
      </div>

      {viewer && project !== null && (
        <Suspense fallback={null}>
          <DualPdfViewer
            key={viewer.opening}
            projectId={project}
            packageId={packageId}
            item={viewer.item}
            aside={viewer.recorded ? <RecordedNumbersPanel projectId={project} packageId={packageId} failure={viewer.recorded} load={recorded} /> : null}
            onClose={() => setViewer(null)}
          />
        </Suspense>
      )}
      </ArchitectProvider>
    </PicturesProvider>
  );
}
