import { useRef, useState } from 'react';
import {
  AlignLeft,
  ArrowLeft,
  BookCheck,
  Check,
  Cpu,
  Cloud,
  FileSearch,
  Gauge,
  GitCompareArrows,
  Layers,
  LayoutPanelLeft,
  ListChecks,
  ListX,
  LoaderCircle,
  Mail,
  MessageCircleQuestion,
  Package,
  Plus,
  ScanSearch,
  Sheet,
  Sigma,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { Command as CommandPrimitive } from 'cmdk';

import { PlanCheck } from '@/components/chat/chat-plan-check';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { ChatFile, ChatFinding, ChatFullCheckPlan, ChatModel, ChatPlanAnswer, ChatProduct } from '@/lib/chat-api';
import { downloadFile, downloadFindingsCsv, markupFailureWords } from '@/lib/chat-exports';
import {
  AREA_PROMPT,
  areaPrompt,
  effortFor,
  FULL_CHECK_MINUTES,
  fullCheckEstimate,
  GROUP_WORDS,
  nextBlank,
  modelActions,
  plusAction,
  plusActions,
  readRecent,
  rememberRecent,
  sameProducts,
  type PlusAction,
  type PlusGroup,
  type PlusIcon,
} from '@/lib/chat-plus-actions';
import { browserStorage, chooseEffort, chooseModel, EFFORT_WORDS, type ChatChoice } from '@/lib/chat-settings';
import { planAnswers, usePlanCheck } from '@/lib/chat-plan-check';
import { useProductCatalog } from '@/lib/products';
import { cn } from '@/lib/utils';

const ICONS: Record<PlusIcon, LucideIcon> = {
  full_check: ScanSearch,
  page: FileSearch,
  area: LayoutPanelLeft,
  rules: BookCheck,
  consistency: ListChecks,
  strings: Sigma,
  missing: ListX,
  rounds: GitCompareArrows,
  cloud: Cloud,
  csv: Sheet,
  letter: Mail,
  rfi: MessageCircleQuestion,
  summary: AlignLeft,
  product: Package,
  both: Layers,
  quick: Zap,
  thorough: Gauge,
  model: Cpu,
};

const GROUPS: readonly PlusGroup[] = ['checks', 'outputs', 'focus', 'model'];

/** A row: claude.ai's roomy menu rows; the picked one is a soft grey. */
const ROW =
  'relative flex w-full cursor-default items-center gap-3 rounded-lg px-2.5 py-[7px] text-left text-sm outline-hidden select-none data-[selected=true]:bg-accent data-[disabled=true]:cursor-default data-[disabled=true]:opacity-50';
const HEADING =
  '[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground';

export interface PlusMenuProps {
  /** Put a prompt into the composer (the page decides how it joins the words already there). */
  onPrompt: (text: string) => void;
  /** The composer's box: focused after a prompt, with its first blank selected. */
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  /** The full check: its confirm line ("Full check · Countertops · Opus 5.5 · High · up to $5"), why it cannot run, and how to run it. */
  fullCheck: {
    summary: string;
    blocked: string | null;
    onRun: (answers?: ChatPlanAnswer[]) => void;
    /** #1569: code's plan and its 0-3 questions, read when the confirm view opens. */
    loadPlan?: () => Promise<ChatFullCheckPlan>;
  };
  /**
   * #1584: the vendor consistency check ($0, by code); null where it cannot run (no chat API for it).
   * `onRun` settles when the stored answer is shown (or says why it could not run).
   */
  consistency?: { onRun: () => Promise<void> } | null;
  /** The newest finished answer with findings, for the $0 files. */
  lastFindings: { messageId: string; findings: ChatFinding[] } | null;
  /** Code draws the answer's findings on a copy of the vendor's drawing (the existing markup-pdf route). */
  makeMarkupPdf: ((messageId: string) => Promise<ChatFile>) | null;
  fileHref: (file: ChatFile) => string;
  products: readonly ChatProduct[] | null;
  onProducts: (products: ChatProduct[]) => void;
  models: readonly ChatModel[];
  choice: ChatChoice | null;
  onChoice: (choice: ChatChoice) => void;
  /** "$5": the most one answer may cost, for the full check's estimate. */
  messageCap: string | null;
  /** Areas the chat's answers named, offered by "Compare an area…". */
  areas: readonly string[];
  /** Something went wrong or cannot be done, in plain words (shown above the composer). */
  onNotice: (words: string) => void;
  /** A message is running: Focus waits (the chips are locked then too). */
  running: boolean;
}

type View = 'list' | 'areas' | 'confirm';

/**
 * The "+" at the left of the composer (#1517), like claude.ai's: a menu that opens upward with a
 * search box on top, grouped Checks / Outputs / Focus, the recently used first. Arrows move, Enter
 * picks, Esc closes (or goes back), typing searches. A question fills the composer with an editable
 * prompt; the full check asks first with the same line as the "Run full check" button; the cloud
 * drawing and the Excel are made by code from the last answer's findings ($0).
 */
export function PlusMenu(props: PlusMenuProps) {
  const { textareaRef, fullCheck, lastFindings, makeMarkupPdf, products, models, choice, running } = props;
  const catalog = useProductCatalog();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<View>('list');
  const [search, setSearch] = useState('');
  const [recent, setRecent] = useState<string[]>(() => readRecent(browserStorage()));
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const [planPicked, setPlanPicked] = useState<Record<string, string>>({});
  const plan = usePlanCheck(fullCheck.loadPlan, open && view === 'confirm');
  // Set when the menu closes to put focus in the composer (not back on the "+").
  const focusBox = useRef<{ text: string } | null>(null);

  function openChange(next: boolean) {
    setOpen(next);
    if (next) {
      setView('list');
      setSearch('');
      setRecent(readRecent(browserStorage()));
    }
  }

  function remember(id: string) {
    setRecent(rememberRecent(browserStorage(), id));
  }

  function prompt(id: string, text: string) {
    remember(id);
    focusBox.current = { text };
    props.onPrompt(text);
    setOpen(false);
  }

  function disabledWhy(action: PlusAction): string | null {
    switch (action.run.kind) {
      case 'full_check':
        return fullCheck.blocked;
      case 'consistency':
        if (!props.consistency) return 'Not available here';
        return running ? 'After this answer' : null;
      case 'cloud':
        if (!makeMarkupPdf) return 'Not available here';
        return lastFindings ? null : 'After an answer with findings';
      case 'csv':
        return lastFindings ? null : 'After an answer with findings';
      case 'products':
        if (action.run.comingLater) return 'Coming later';
        return running ? 'After this answer' : null;
      case 'effort':
        if (!choice) return 'Loading the models…';
        return effortFor(models, choice, action.run.level) ? null : 'This model does not offer it';
      case 'model':
        if (!choice) return 'Loading the models…';
        return running ? 'After this answer' : null;
      default:
        return null;
    }
  }

  function picked(action: PlusAction): boolean {
    if (action.run.kind === 'products') return sameProducts(products, action.run.products);
    if (action.run.kind === 'effort' && choice) return effortFor(models, choice, action.run.level) === choice.effort;
    if (action.run.kind === 'model' && choice) return choice.model === action.run.model;
    return false;
  }

  function choose(action: PlusAction) {
    if (disabledWhy(action)) return;
    const run = action.run;
    switch (run.kind) {
      case 'prompt':
        if (action.id === 'compare_area' && props.areas.length) {
          setView('areas');
          setSearch('');
          return;
        }
        prompt(action.id, run.text);
        return;
      case 'full_check':
        setView('confirm');
        return;
      case 'consistency': {
        const check = props.consistency;
        if (!check) return;
        remember(action.id);
        setOpen(false);
        setBusy('Checking the vendor’s numbers…');
        setStatus('Checking the vendor’s numbers');
        check.onRun().finally(() => {
          setBusy(null);
          setStatus('Vendor consistency check finished');
        });
        return;
      }
      case 'cloud':
        if (!lastFindings || !makeMarkupPdf) return;
        remember(action.id);
        setOpen(false);
        setBusy('Making the cloud drawing…');
        setStatus('Making the cloud drawing');
        makeMarkupPdf(lastFindings.messageId).then(
          (file) => {
            setBusy(null);
            setStatus('Cloud drawing ready');
            downloadFile(props.fileHref(file), file.name);
          },
          (error: unknown) => {
            setBusy(null);
            setStatus('');
            props.onNotice(markupFailureWords(error).replace('marked-up PDF', 'cloud drawing'));
          },
        );
        return;
      case 'csv':
        if (!lastFindings) return;
        remember(action.id);
        setOpen(false);
        downloadFindingsCsv(lastFindings.findings, `findings-${lastFindings.messageId.slice(0, 8)}.csv`);
        setStatus('Excel (CSV) downloaded');
        return;
      case 'products':
        remember(action.id);
        props.onProducts([...run.products]);
        setOpen(false);
        return;
      case 'effort': {
        if (!choice) return;
        const effort = effortFor(models, choice, run.level);
        if (!effort) return;
        remember(action.id);
        props.onChoice(chooseEffort(models, choice, effort));
        setStatus(`Effort: ${EFFORT_WORDS[effort]}`);
        setOpen(false);
        return;
      }
      case 'model': {
        if (!choice) return;
        const next = chooseModel(models, choice, run.model);
        props.onChoice(next);
        setStatus(`Model: ${action.label}`);
        setOpen(false);
        return;
      }
    }
  }

  const menuActions = [...plusActions(catalog), ...modelActions(models)];
  const recentActions = recent.map((id) => plusAction(id, catalog)).filter((action): action is PlusAction => action !== null);

  const row = (action: PlusAction, value: string) => {
    const Icon = ICONS[action.icon];
    const why = disabledWhy(action);
    const on = picked(action);
    const estimate = action.run.kind === 'full_check' ? fullCheckEstimate(props.messageCap) : action.estimate;
    // Focus rows say what they set; the effort rows name the effort they will pick.
    const hint =
      action.run.kind === 'effort' && choice && !why
        ? `${EFFORT_WORDS[effortFor(models, choice, action.run.level)!]} effort · ${action.run.level === 'quick' ? 'faster and cheaper' : 'slower, more careful'}`
        : action.hint;
    return (
      <CommandPrimitive.Item
        key={value}
        value={value}
        keywords={[action.label, ...action.keywords]}
        disabled={why !== null}
        onSelect={() => choose(action)}
        data-action={action.id}
        className={ROW}
      >
        <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="min-w-0 flex-1 truncate">{action.label}</span>
            {!why && estimate && (
              <span data-slot="chat-plus-estimate" className="shrink-0 text-xs whitespace-nowrap text-muted-foreground">
                <Estimate words={estimate} />
              </span>
            )}
          </span>
          <span className="truncate text-xs text-muted-foreground">{why ?? hint}</span>
        </span>
        {on && <Check className="size-4 shrink-0" aria-label="Current" />}
      </CommandPrimitive.Item>
    );
  };

  return (
    <>
      <Popover open={open} onOpenChange={openChange}>
        <PopoverTrigger
          type="button"
          aria-label="Add a check or output"
          title="Checks, outputs and focus"
          aria-busy={busy ? true : undefined}
          data-slot="chat-plus"
          className="grid size-8 shrink-0 place-items-center rounded-lg text-foreground/75 transition-colors duration-150 hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none data-[state=open]:bg-accent data-[state=open]:text-foreground motion-reduce:transition-none"
        >
          {busy ? (
            <LoaderCircle className="size-4 motion-safe:animate-spin" aria-hidden="true" />
          ) : (
            <Plus className="size-[18px] transition-transform duration-150 in-data-[state=open]:rotate-45 motion-reduce:transition-none" aria-hidden="true" />
          )}
        </PopoverTrigger>
        <PopoverContent
          // Always above the composer (#1538): never down over the suggestions and the disclaimer.
          // It takes the room above (Radix's available height) and scrolls its list inside.
          side="top"
          align="start"
          sideOffset={10}
          collisionPadding={12}
          avoidCollisions={false}
          aria-label="Checks, outputs and focus"
          data-slot="chat-plus-menu"
          onCloseAutoFocus={(event) => {
            const wanted = focusBox.current;
            focusBox.current = null;
            if (!wanted) return;
            // A prompt went into the composer: focus it there, with its first blank picked.
            event.preventDefault();
            const box = textareaRef.current;
            if (!box) return;
            box.focus();
            const at = box.value.length - wanted.text.length;
            const blank = nextBlank(box.value, Math.max(0, at));
            if (blank && blank[0] >= at) box.setSelectionRange(blank[0], blank[1]);
            else box.setSelectionRange(box.value.length, box.value.length);
          }}
          className="flex max-h-(--radix-popover-content-available-height) w-[24rem] max-w-[calc(100vw-2.5rem)] flex-col overflow-hidden rounded-xl p-0 font-sans shadow-lg duration-120 motion-reduce:animate-none"
        >
          {view === 'confirm' ? (
            <div data-slot="chat-plus-confirm" className="grid gap-3 p-3">
              <p data-slot="chat-full-check-summary" className="text-sm font-medium tabular-nums">{fullCheck.summary}</p>
              <p className="text-xs text-muted-foreground">
                Checks every page against the architect and GV’s rules, then code draws the cloud drawing and the check report. About {FULL_CHECK_MINUTES} minutes.
              </p>
              <PlanCheck state={plan} picked={planPicked} onPick={(id, value) => setPlanPicked((now) => ({ ...now, [id]: value }))} />
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setView('list')}
                  className="h-8 rounded-lg border bg-background px-3 text-sm hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                >
                  Back
                </button>
                <button
                  type="button"
                  autoFocus
                  onClick={() => {
                    remember('full_check');
                    const answers = planAnswers(plan.status === 'ready' ? plan.plan.doubts : [], planPicked);
                    setOpen(false);
                    setPlanPicked({});
                    if (answers.length) fullCheck.onRun(answers);
                    else fullCheck.onRun();
                  }}
                  className="h-8 rounded-lg bg-primary px-3 text-sm font-medium text-primary-foreground hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
                >
                  Run full check
                </button>
              </div>
            </div>
          ) : (
            <CommandPrimitive
              label={view === 'areas' ? 'Pick an area' : 'Checks, outputs and focus'}
              loop
              filter={matches}
              className="flex max-h-[32rem] min-h-0 flex-1 flex-col"
              onKeyDown={(event) => {
                // Back out of the area list with Esc, or Backspace in an empty search.
                if (view === 'areas' && (event.key === 'Escape' || (event.key === 'Backspace' && !search))) {
                  event.preventDefault();
                  event.stopPropagation();
                  setView('list');
                  setSearch('');
                }
              }}
            >
              <div className="flex shrink-0 items-center gap-2 border-b px-3">
                {view === 'areas' ? (
                  <button
                    type="button"
                    aria-label="Back to all actions"
                    onClick={() => {
                      setView('list');
                      setSearch('');
                    }}
                    className="-ml-1 grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                  >
                    <ArrowLeft className="size-4" aria-hidden="true" />
                  </button>
                ) : null}
                <CommandPrimitive.Input
                  // A new box for each view, so moving to the area list puts the cursor in its search.
                  key={view}
                  value={search}
                  onValueChange={setSearch}
                  autoFocus
                  placeholder={view === 'areas' ? 'Search areas' : 'Search checks and outputs'}
                  aria-label={view === 'areas' ? 'Search areas' : 'Search checks and outputs'}
                  className="h-11 w-full min-w-0 bg-transparent text-sm outline-hidden placeholder:text-muted-foreground"
                />
              </div>
              {/* The list scrolls on its own, under the search box (never beneath it); scroll padding keeps a
                  picked row clear of the group heading above it. */}
              <CommandPrimitive.List className={cn('min-h-0 flex-1 scroll-pt-9 scroll-pb-2 overflow-x-hidden overflow-y-auto overscroll-contain p-1.5', HEADING)}>
                <CommandPrimitive.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">Nothing matches “{search}”.</CommandPrimitive.Empty>
                {view === 'areas' ? (
                  <CommandPrimitive.Group heading="Areas in this chat’s answers">
                    {props.areas.map((area) => (
                      <CommandPrimitive.Item key={area} value={`area:${area}`} keywords={[area]} onSelect={() => prompt('compare_area', areaPrompt(area))} className={ROW}>
                        <LayoutPanelLeft className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                        <span className="min-w-0 flex-1 truncate">{area}</span>
                      </CommandPrimitive.Item>
                    ))}
                    <CommandPrimitive.Item
                      value="area:other"
                      keywords={['other', 'type', 'another']}
                      onSelect={() => prompt('compare_area', AREA_PROMPT)}
                      className={ROW}
                      forceMount
                    >
                      <span className="size-4 shrink-0" aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate text-muted-foreground">Type another area…</span>
                    </CommandPrimitive.Item>
                  </CommandPrimitive.Group>
                ) : (
                  <>
                    {!search && recentActions.length > 0 && (
                      <CommandPrimitive.Group heading="Recent">{recentActions.map((action) => row(action, `recent:${action.id}`))}</CommandPrimitive.Group>
                    )}
                    {GROUPS.map((group) => (
                      <CommandPrimitive.Group key={group} heading={GROUP_WORDS[group]}>
                        {menuActions.filter((action) => action.group === group).map((action) => row(action, action.id))}
                      </CommandPrimitive.Group>
                    ))}
                  </>
                )}
              </CommandPrimitive.List>
            </CommandPrimitive>
          )}
        </PopoverContent>
      </Popover>
      <span className="sr-only" data-slot="chat-plus-status" aria-live="polite">{status}</span>
    </>
  );
}

/**
 * The search: every word typed must appear in the option's name or its other words (no fuzzy
 * letter-skipping, so "rfi" finds only the RFI list); names that start with it come first.
 */
function matches(value: string, search: string, keywords?: string[]): number {
  const words = search.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return 1;
  const hay = (keywords ?? [value]).join(' ').toLowerCase();
  if (!words.every((word) => hay.includes(word))) return 0;
  return (keywords?.[0] ?? value).toLowerCase().startsWith(words[0]) ? 1 : 0.5;
}

/** "~2 min · ~$0.50", in the words' own face with even-width digits (no typewriter text). */
function Estimate({ words }: { words: string }) {
  return <span className="font-sans tabular-nums">{words}</span>;
}
