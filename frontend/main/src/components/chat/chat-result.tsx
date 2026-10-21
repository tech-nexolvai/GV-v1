import { useId, useState } from 'react';
import { ArrowRight, Check, ChevronRight, CircleDashed, Download, ExternalLink, FileCheck2, FileText, ListMinus, Minus, Package, RefreshCw, RotateCw, Stamp, X } from 'lucide-react';

import { CloudPreview, type CloudReview } from '@/components/chat/chat-full-check';
import { Collapse, GVSpark } from '@/components/chat/chat-motion';
import type { ChatFile, ChatFinding, ChatPackageState, ChatReviewAction, ChatTimings, FindingCategory, FindingDecision } from '@/lib/chat-api';
import { chatPaths } from '@/lib/chat-api';
import { categoriesPresent, categoryWords, sideLabels } from '@/lib/chat-findings';
import { fileSize, openHref } from '@/lib/chat-messages';
import {
  commentNumber,
  decisionsWords,
  differenceWords,
  filesLine,
  itemParts,
  neededWords,
  notComparedGroups,
  previewPageOf,
  resultGroups,
  resultTabs,
  shouldBeValue,
  TAB_WORDS,
  vendorValue,
  verdictOf,
  workLine,
  workSteps,
  type ResultTab,
  type WorkLineInput,
} from '@/lib/chat-result';
import { PROOF_WORDS, ruleNoteWords } from '@/lib/chat-proof';
import { inchWords, type RoundCounts } from '@/lib/chat-trust';
import { ROUND_WORDS } from '@/lib/words';
import { cn } from '@/lib/utils';

/**
 * A finished full check, summary first (#1717, UI redesign step 2; design spec 4.4):
 *
 * 1. the work line: what the check did and how long (the only place for the time), opening to its steps;
 * 2. the result head: the cloud drawing's page, the review result in AWI's words, one sentence that
 *    says only what no count says, the main action and the package's files in one line;
 * 3. tiles that ARE the tabs, the only place for the counts: Wrong / Right / Not compared, and
 *    Ask the architect when the architect's size breaks a GV rule (#1839: decided, never red);
 * 4. the open tab's rows, the evidence beside each; a row opens the evidence panel.
 *
 * No Agree / Not a problem on the rows (owner decision 3): the optional override is in the panel.
 */

export interface ResultFiles {
  cloud: ChatFile | null;
  report: ChatFile | null;
  notCompared: readonly ChatFile[];
  pkg: ChatFile | null;
}

export interface FullCheckResultProps {
  findings: readonly ChatFinding[];
  decisionOf: (finding: ChatFinding) => FindingDecision | null;
  chatId: string;
  files: ResultFiles;
  reviewAction: ChatReviewAction | null;
  packageState: ChatPackageState | null;
  work: Omit<WorkLineInput, 'filesMade'> & { timings: ChatTimings | null };
  pictureUrl: (pictureId: string) => string;
  onOpen: (findingId: string) => void;
  /** Run the full check again (an unfinished check's main action); absent where it cannot. */
  onRunAgain?: (() => void) | null;
  /** #1525: the reviewer's overrides so far, and "Update cloud drawing". */
  review?: CloudReview | null;
  /** The code's text summary, folded under "Summary as text". */
  summary?: React.ReactNode;
  /** What the AI did (its activity rows), shown inside the work line's fold. */
  activity?: React.ReactNode;
  /** #1744: a later round's counts (Closed / Previous comment not addressed / New); null with one round. */
  rounds?: RoundCounts | null;
}

export function FullCheckResult(props: FullCheckResultProps) {
  const { decisionOf, chatId, files, reviewAction, packageState, work, pictureUrl, onOpen, onRunAgain = null, review = null, summary = null, activity = null, rounds = null } = props;
  // The reviewer's newest decision laid onto each finding, so its row shows what it changed (#1803: the reviewer's "should be").
  const findings = props.findings.map((finding) => {
    const decision = decisionOf(finding);
    return decision && decision !== finding.decision ? { ...finding, decision } : finding;
  });
  const groups = resultGroups(findings, decisionOf);
  const [tab, setTab] = useState<ResultTab>(groups.wrong.length ? 'wrong' : groups.ruleNotes.length ? 'rule_note' : groups.notCompared.length ? 'not_compared' : 'right');
  const [category, setCategory] = useState<FindingCategory | null>(null);
  // #1803: what the reviewer marked Right is Right, as the vendor's files say ("Right (reviewer)"), listed first under Right.
  const counts: Record<ResultTab, number> = { wrong: groups.wrong.length, right: groups.right.length + groups.dismissed.length, rule_note: groups.ruleNotes.length, not_compared: groups.notCompared.length };
  const listOf: Record<ResultTab, ChatFinding[]> = { wrong: groups.wrong, right: [...groups.dismissed, ...groups.right], rule_note: groups.ruleNotes, not_compared: groups.notCompared };
  // #1839: the fourth tile only when a size the architect specifies breaks a GV rule.
  const tabs = resultTabs(groups);
  const inTab = listOf[tab];
  const categories = categoriesPresent(inTab);
  const shown = category ? inTab.filter((finding) => (finding.category ?? 'other') === category) : inTab;
  const filesMade = filesLine(files).count;
  const line = workLine({ ...work, filesMade });
  const tabsId = useId();

  return (
    <section data-slot="chat-result" aria-label="Full check result" className="chat-wide grid min-w-0 gap-4">
      <WorkLine words={line.words} time={line.time} timings={work.timings} activity={activity} />
      <ResultHead files={files} chatId={chatId} reviewAction={reviewAction} previewPage={previewPageOf(groups)} onRunAgain={onRunAgain} review={review} />

      {rounds && <RoundStrip rounds={rounds} />}

      <div role="tablist" aria-label="Items" data-slot="chat-result-tiles" className={cn('grid gap-2.5 max-md:gap-2', tabs.length === 4 ? 'grid-cols-4 max-md:grid-cols-2' : 'grid-cols-3')}>
        {tabs.map((each) => (
          <button
            key={each}
            type="button"
            role="tab"
            id={`${tabsId}-${each}`}
            aria-selected={tab === each}
            aria-controls={`${tabsId}-panel`}
            data-tab={each}
            onClick={() => { setTab(each); setCategory(null); }}
            className={cn(
              'grid min-w-0 content-start gap-1 rounded-[14px] border bg-card px-4 py-3 text-left transition-[border-color,box-shadow] duration-200 ease-out hover:border-foreground/30 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none max-md:px-2.5 max-md:py-2',
              tab === each && 'border-foreground shadow-[0_0_0_1px_var(--foreground)] hover:border-foreground',
            )}
          >
            <span className="flex min-w-0 items-center gap-2 text-sm font-semibold max-md:text-xs"><OutcomeGlyph kind={each} />{TAB_WORDS[each].word}</span>
            <span data-slot="chat-result-count" className={cn('num text-[28px] leading-9 font-medium tracking-[-0.02em] max-md:text-2xl max-md:leading-8', each === 'wrong' ? 'text-outcome-fail-fg' : each === 'right' ? 'text-outcome-pass-fg' : each === 'rule_note' ? 'text-outcome-ask-fg' : 'text-outcome-missing-fg')}>{counts[each]}</span>
            <span className="text-[12.5px] leading-[18px] text-muted-foreground max-md:hidden">{TAB_WORDS[each].hint}</span>
          </button>
        ))}
      </div>

      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p className="text-[13px] text-muted-foreground">
          <span className="font-medium text-foreground">{tab === 'wrong' ? 'Biggest first' : tab === 'not_compared' ? 'By reason' : 'By page'}</span>
          {tab === 'wrong' ? ' · what changes fabrication most' : tab === 'not_compared' ? ' · and what is needed to compare' : tab === 'rule_note' ? ' · with the question for the architect' : ''}
        </p>
        {categories.length > 1 && (
          <div role="group" aria-label="Product" className="inline-flex rounded-lg bg-muted p-0.5">
            {[null, ...categories].map((each) => (
              <button
                key={each ?? 'all'}
                type="button"
                aria-pressed={category === each}
                onClick={() => setCategory(each)}
                className={cn('h-7 rounded-md px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none', category === each ? 'bg-card text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.08)]' : 'text-muted-foreground hover:text-foreground')}
              >
                {each ? categoryWords(each) : 'All'}
              </button>
            ))}
          </div>
        )}
      </div>

      <div id={`${tabsId}-panel`} role="tabpanel" aria-labelledby={`${tabsId}-${tab}`} data-slot="chat-result-list" data-tab={tab} key={tab} className="chat-fade-in min-w-0 overflow-hidden rounded-[14px] border bg-card">
        {shown.length === 0 && <p className="px-4 py-6 text-center text-sm text-muted-foreground">{inTab.length ? 'Nothing for this product.' : tab === 'wrong' ? 'Nothing proved wrong.' : tab === 'right' ? 'Nothing matched yet.' : tab === 'rule_note' ? 'No architect’s size breaks a GV rule.' : 'Every item was compared.'}</p>}
        {tab === 'wrong' && !rounds && shown.filter((finding) => !groups.dismissed.includes(finding)).map((finding) => (
          <WrongRow key={finding.id} finding={finding} number={commentNumber(finding, packageState)} pictureUrl={pictureUrl} onOpen={onOpen} />
        ))}
        {/* #1744: in a later round, what the vendor must fix first, then what is new, then what was closed. */}
        {tab === 'wrong' && rounds && roundGroups(shown.filter((finding) => !groups.dismissed.includes(finding)), category ? groups.right.filter((finding) => (finding.category ?? 'other') === category) : groups.right).map((group) => (
          <div key={group.key} data-slot="chat-result-round" data-round={group.key}>
            <GroupHead words={group.words} note={group.note} count={group.findings.length} />
            {group.findings.map((finding) => (group.key === 'closed'
              ? <CompactRow key={finding.id} kind="right" finding={finding} onOpen={onOpen} />
              : <WrongRow key={finding.id} finding={finding} number={commentNumber(finding, packageState)} pictureUrl={pictureUrl} onOpen={onOpen} />))}
          </div>
        ))}
        {tab === 'right' && shown.some((finding) => groups.dismissed.includes(finding)) && (
          <>
            <GroupHead words="Right (your call)" note="marked Right by the reviewer" count={shown.filter((finding) => groups.dismissed.includes(finding)).length} />
            {shown.filter((finding) => groups.dismissed.includes(finding)).map((finding) => <CompactRow key={finding.id} kind="dismissed" finding={finding} onOpen={onOpen} />)}
            {shown.some((finding) => !groups.dismissed.includes(finding)) && <GroupHead words="Right" note="matches the architect or GV’s rules" count={shown.filter((finding) => !groups.dismissed.includes(finding)).length} />}
          </>
        )}
        {tab === 'right' && shown.filter((finding) => !groups.dismissed.includes(finding)).map((finding) => <CompactRow key={finding.id} kind="right" finding={finding} onOpen={onOpen} />)}
        {tab === 'rule_note' && shown.length > 0 && (
          <div data-slot="chat-result-rule-notes">
            <GroupHead words={PROOF_WORDS.rule_note} note="nothing is asked of the vendor" count={shown.length} kind="rule_note" />
            {shown.map((finding) => <CompactRow key={finding.id} kind="rule_note" finding={finding} onOpen={onOpen} />)}
          </div>
        )}
        {tab === 'not_compared' && notComparedGroups(shown).map((group) => (
          <div key={group.reason} data-slot="chat-result-reason" data-reason={group.reason}>
            <GroupHead words={group.words} count={group.findings.length} />
            {group.findings.map((finding) => <CompactRow key={finding.id} kind="not_compared" finding={finding} onOpen={onOpen} />)}
          </div>
        ))}
      </div>

      {summary && <SummaryFold>{summary}</SummaryFold>}
    </section>
  );
}

// ── The outcome glyph: colour never alone ─────────────────────

export function OutcomeGlyph({ kind, className }: { kind: ResultTab | 'dismissed'; className?: string }) {
  if (kind === 'wrong') return <span aria-hidden="true" className={cn('grid size-[18px] shrink-0 place-items-center rounded-full bg-outcome-fail text-white', className)}><X className="size-3" strokeWidth={2.75} /></span>;
  if (kind === 'right') return <span aria-hidden="true" className={cn('grid size-[18px] shrink-0 place-items-center rounded-full bg-outcome-pass text-white', className)}><Check className="size-3" strokeWidth={2.75} /></span>;
  // #1839: decided, nothing for the vendor: a filled slate circle with a question mark (never amber).
  if (kind === 'rule_note') return <span aria-hidden="true" className={cn('grid size-[18px] shrink-0 place-items-center rounded-full bg-outcome-ask text-xs leading-none font-bold text-background', className)}>?</span>;
  return <span aria-hidden="true" className={cn('grid size-[18px] shrink-0 place-items-center rounded-full border-[1.5px] border-dashed border-outcome-missing text-outcome-missing-fg', className)}><Minus className="size-2.5" strokeWidth={2.75} /></span>;
}

// ── The work line ─────────────────────────────────────────

function WorkLine({ words, time, timings, activity }: { words: string; time: string | null; timings: ChatTimings | null; activity: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const steps = workSteps(timings);
  const foldable = steps.length > 0 || Boolean(activity);
  return (
    <div data-slot="chat-work-line" className="grid min-w-0 gap-1">
      <button
        type="button"
        aria-expanded={foldable ? open : undefined}
        aria-controls={foldable ? id : undefined}
        disabled={!foldable}
        onClick={() => setOpen(!open)}
        className="group/work inline-flex w-fit max-w-full min-w-0 items-center gap-2.5 rounded-md py-0.5 text-left text-[13.5px] text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:cursor-default disabled:hover:text-muted-foreground"
      >
        <GVSpark state="done" size={18} />
        <span className="min-w-0">{words}{time && <> · <span className="num">{time}</span></>}</span>
        {foldable && <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')} />}
      </button>
      {foldable && (
        <Collapse open={open} id={id}>
          <div className="grid min-w-0 gap-3 pt-2 pb-1 pl-7">
            {steps.length > 0 && (
              <ol data-slot="chat-work-steps" className="grid gap-1.5 border-l pl-4 text-[13px]">
                {steps.map((step) => (
                  <li key={step.words} className="flex min-w-0 items-baseline gap-2">
                    <span className="min-w-0 text-foreground">{step.words}</span>
                    {step.time && <span className="num shrink-0 text-xs text-muted-foreground">{step.time}</span>}
                  </li>
                ))}
              </ol>
            )}
            {activity}
          </div>
        </Collapse>
      )}
    </div>
  );
}

// ── The result head ───────────────────────────────────────

function ResultHead({ files, chatId, reviewAction, previewPage, onRunAgain, review }: { files: ResultFiles; chatId: string; reviewAction: ChatReviewAction | null; previewPage: number; onRunAgain: (() => void) | null; review: CloudReview | null }) {
  const [filesOpen, setFilesOpen] = useState(false);
  const filesId = useId();
  const verdict = verdictOf(reviewAction);
  const href = (file: ChatFile) => file.url || chatPaths.file(chatId, file.id);
  const line = filesLine(files);
  if (!verdict && !files.cloud && !files.pkg && !line.count) return null;
  const primary = verdict?.draft
    ? onRunAgain && <button type="button" data-slot="chat-result-run-again" onClick={onRunAgain} className={BUTTON_PRIMARY}><RotateCw className="size-4" aria-hidden="true" />Run the check again</button>
    : files.pkg ? <a data-slot="chat-package-download" href={href(files.pkg)} download={files.pkg.name} aria-label={`Download package: ${files.pkg.name}`} className={BUTTON_PRIMARY}><Download className="size-4" aria-hidden="true" />Download package</a>
      : files.cloud && <a data-slot="chat-cloud-download" href={href(files.cloud)} download={files.cloud.name} aria-label={`Download ${files.cloud.name}`} className={BUTTON_PRIMARY}><Download className="size-4" aria-hidden="true" />Download cloud drawing</a>;
  // #1803: every decision makes the whole package again (the cloud drawing, the letter, the
  // register, the Not-compared list). Until it holds them all, no file of the old one is handed out.
  const stale = Boolean(review?.stale);
  const decided = review?.decided ?? 0;
  const remake = review?.onUpdate && decided > 0
    ? <button type="button" data-slot="chat-package-remake" onClick={review.onUpdate} disabled={review.updating} className={cn(stale && !review.updating ? BUTTON_PRIMARY : BUTTON, 'disabled:cursor-progress disabled:opacity-70')}><RefreshCw className={cn('size-4', review.updating && 'animate-spin motion-reduce:animate-none')} aria-hidden="true" />{review.updating ? 'Making the package again…' : 'Re-make the package'}</button>
    : null;
  const shownPrimary = stale && !verdict?.draft ? null : primary;
  const secondary = stale ? null : verdict?.draft
    ? files.pkg && <a data-slot="chat-package-download" href={href(files.pkg)} download={files.pkg.name} aria-label={`Download draft package: ${files.pkg.name}`} className={BUTTON}><Download className="size-4" aria-hidden="true" />Download draft</a>
    : files.cloud && <a href={openHref(files.cloud, href(files.cloud))} target="_blank" rel="noopener noreferrer" className={BUTTON}><ExternalLink className="size-4" aria-hidden="true" />Open cloud drawing</a>;
  return (
    <section data-slot="chat-result-head" aria-label="Review result" className="chat-rise grid min-w-0 overflow-hidden rounded-[14px] border bg-card md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      {files.cloud && !stale && (
        <a href={openHref(files.cloud, href(files.cloud))} target="_blank" rel="noopener noreferrer" aria-label="Open the cloud drawing" className="grid min-w-0 content-center border-r bg-muted/40 p-3.5 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset max-md:hidden">
          <CloudPreview href={href(files.cloud)} page={previewPage} total={null} />
        </a>
      )}
      {files.cloud && stale && (
        <div aria-hidden="true" className="grid min-w-0 place-items-center border-r bg-muted/40 p-3.5 text-[13px] text-muted-foreground max-md:hidden">
          <RefreshCw className={cn('size-5', review?.updating && 'animate-spin motion-reduce:animate-none')} />
        </div>
      )}
      <div className="grid min-w-0 content-start gap-3 p-5 max-md:p-4">
        {verdict && (
          <>
            <p className="text-xs font-medium text-muted-foreground">Review result</p>
            <p
              data-slot="chat-review-action"
              data-action={reviewAction?.action}
              role={verdict.draft ? 'status' : undefined}
              className={cn('inline-flex w-fit max-w-full items-center gap-2 rounded-[7px] border-[1.5px] px-2.5 py-1 text-[12.5px] font-semibold tracking-[0.06em] uppercase', verdict.draft ? 'border-dashed border-muted-foreground text-muted-foreground' : 'border-foreground text-foreground')}
            >
              {verdict.draft ? <CircleDashed className="size-4 shrink-0" aria-hidden="true" /> : <Stamp className="size-4 shrink-0" aria-hidden="true" />}
              {verdict.stamp}
            </p>
            <p data-slot="chat-review-sentence" className="max-w-prose text-[14.5px] leading-relaxed text-muted-foreground">{verdict.sentence}</p>
          </>
        )}
        {(shownPrimary || secondary || remake) && (
          <div className="flex flex-wrap gap-2">
            {shownPrimary}
            {secondary}
            {remake}
          </div>
        )}
        {review && decided > 0 && <PackageLine review={review} />}
        {line.count > 0 && !stale && (
          <div className="grid min-w-0 gap-1 border-t pt-3">
            <button type="button" aria-expanded={filesOpen} aria-controls={filesId} onClick={() => setFilesOpen(!filesOpen)} data-slot="chat-result-files" className="inline-flex max-w-full min-w-0 items-center gap-2 rounded-md text-left text-[13px] text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
              <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', filesOpen && 'rotate-90')} />
              <span className="min-w-0 truncate"><span className="font-medium text-foreground">{line.count} {line.count === 1 ? 'file' : 'files'}:</span> {line.words}</span>
            </button>
            <Collapse open={filesOpen} id={filesId}>
              <ul data-slot="chat-result-file-rows" className="grid gap-0.5 pt-1.5">
                {files.cloud && <FileRow icon={FileText} name="GV cloud drawing" file={files.cloud} href={href(files.cloud)} />}
                {files.pkg && <FileRow icon={Package} name="Package (.zip)" file={files.pkg} href={href(files.pkg)} note="the cloud drawing, the comment letter, the issue register and the lists" />}
                {files.notCompared.map((file) => <FileRow key={file.id} icon={ListMinus} name={`Not-compared list (${/\.pdf$/i.test(file.name) ? 'PDF' : /\.csv$/i.test(file.name) ? 'CSV' : 'Excel'})`} file={file} href={href(file)} />)}
                {files.report && <FileRow icon={FileCheck2} name="Check report" file={files.report} href={href(files.report)} />}
              </ul>
            </Collapse>
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * #1803: what the package holds after the reviewer's decisions: "Making the package again with your
 * 2 decisions…", "Package updated with your 2 decisions", or that it could not be made (no download then).
 */
export function PackageLine({ review }: { review: CloudReview }) {
  const words = decisionsWords(review.decided);
  if (review.updating) {
    return <p role="status" data-slot="chat-package-line" data-state="updating" className="inline-flex items-center gap-2 text-[13px] text-muted-foreground"><RefreshCw className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />Making the package again with your {words}…</p>;
  }
  if (review.failed) {
    return <p role="alert" data-slot="chat-package-line" data-state="failed" className="text-[13px] text-muted-foreground">{review.refusal ?? `The package could not be made again with your ${words}. The old one is not offered: re-make the package.`}</p>;
  }
  if (review.stale) {
    return <p role="status" data-slot="chat-package-line" data-state="outdated" className="text-[13px] text-muted-foreground">The package does not hold your newest decisions yet. Re-make the package.</p>;
  }
  return (
    <p role="status" data-slot="chat-package-line" data-state="updated" className="text-[13px] text-muted-foreground">
      Package updated with your {words}{review.updatedAt ? <> · <span className="num">{review.updatedAt}</span></> : null} · the cloud drawing, letter, register and Not-compared list include them
    </p>
  );
}

const BUTTON_PRIMARY = 'inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none active:scale-[0.97] motion-reduce:active:scale-100';
const BUTTON = 'inline-flex h-9 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none';

function FileRow({ icon: Icon, name, file, href, note }: { icon: typeof FileText; name: string; file: ChatFile; href: string; note?: string }) {
  const pdf = /\.pdf$/i.test(file.name) || file.mime === 'application/pdf';
  return (
    <li className="flex min-w-0 items-center gap-2.5 py-1 text-[13px]">
      <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><Icon className="size-3.5" aria-hidden="true" /></span>
      <span className="min-w-0 flex-1 truncate" title={file.name}><span className="font-medium">{name}</span>{note && <span className="text-muted-foreground max-md:hidden"> · {note}</span>}</span>
      <span className="num shrink-0 text-xs text-muted-foreground">{fileSize(file.size)}</span>
      {pdf && <a href={openHref(file, href)} target="_blank" rel="noopener noreferrer" aria-label={`Open ${file.name}`} className="rounded-md px-1.5 py-0.5 text-[13px] text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Open</a>}
      <a href={href} download={file.name} aria-label={`Download ${file.name}`} className="rounded-md px-1.5 py-0.5 text-[13px] text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Download</a>
    </li>
  );
}

// ── Rows ──────────────────────────────────────────────────

function GroupHead({ words, count, kind, note }: { words: string; count: number; kind?: ResultTab; note?: string }) {
  return (
    <p data-slot="chat-result-group" data-kind={kind} className="flex items-center justify-between gap-3 border-t bg-muted/40 px-4 py-2.5 text-[13px] font-semibold first:border-t-0">
      <span className="flex min-w-0 items-center gap-2">
        {kind && <OutcomeGlyph kind={kind} className="size-4 [&_svg]:size-2.5" />}
        <span className="min-w-0">{words}{note && <span className="font-normal text-muted-foreground"> · {note}</span>}</span>
      </span>
      <span className="num font-medium text-muted-foreground">{count}</span>
    </p>
  );
}

function Where({ finding, number }: { finding: ChatFinding; number?: string | null }) {
  const { area } = itemParts(finding.item);
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-muted-foreground">
      {number && (
        <span data-slot="chat-result-number" className="num inline-flex h-[22px] items-center gap-1 rounded-full border border-outcome-fail-fg/25 bg-outcome-fail-bg pr-2 pl-1 text-xs font-semibold text-outcome-fail-fg">
          <OutcomeGlyph kind="wrong" className="size-3.5 [&_svg]:size-2.5" />{number}
        </span>
      )}
      {area && <span className="min-w-0">{area}</span>}
      <span className="num text-xs">p.{finding.page}</span>
      {finding.round_match?.words && (
        // Information only (2026-10-22): the vendor's own earlier round is never counted as right.
        <span data-slot="chat-result-round-match" className="rounded border border-dashed px-1 text-xs leading-4">{finding.round_match.words}</span>
      )}
    </span>
  );
}

function ValuesLine({ finding }: { finding: ChatFinding }) {
  const vendor = vendorValue(finding);
  const should = shouldBeValue(finding);
  const difference = differenceWords(finding);
  return (
    <p data-slot="chat-result-values" className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1 text-[13px] text-muted-foreground">
      <span className="inline-flex items-baseline gap-1.5">Vendor <span className="num text-[15px] font-semibold text-outcome-fail-fg">{vendor.main}</span>{vendor.note && <span className="num text-xs">{vendor.note}</span>}</span>
      {should && (
        <>
          <ArrowRight className="size-3.5 shrink-0 self-center text-muted-foreground/70" aria-label="should be" />
          <span className="inline-flex items-baseline gap-1.5">Should be <span className="num text-[15px] font-semibold text-outcome-pass-fg">{should.main}</span>{should.note && <span className="text-xs">{should.note}</span>}</span>
        </>
      )}
      {difference && <span className="num rounded-md bg-muted px-1.5 py-0.5 text-xs">{difference}</span>}
    </p>
  );
}

/** The evidence beside a Wrong row: the two crops, or code's sum of the vendor's own parts, or GV's rule. No empty boxes. */
function RowEvidence({ finding, pictureUrl }: { finding: ChatFinding; pictureUrl: (pictureId: string) => string }) {
  const crops: React.ReactNode[] = [];
  // The decided side names (#1581): a vendor's own string or span is never labelled "Architect".
  const labels = sideLabels(finding);
  if (finding.vendor_picture_id) crops.push(<Crop key="v" src={pictureUrl(finding.vendor_picture_id)} who={labels.vendor} page={finding.vendor_box?.page ?? finding.page} />);
  else if (finding.vendor && !/\d/.test(finding.vendor)) crops.push(<WordsBox key="v" who={labels.vendor}>{finding.vendor}</WordsBox>);
  if (finding.parts?.length) {
    const sum = finding.parts_sum ? inchWords(Number(finding.parts_sum)) : null;
    crops.push(
      <WordsBox key="s" who="Added up by code">
        <span className="num line-clamp-3 text-xs leading-[17px]">{finding.parts.map((part) => part.replace(/"/g, '″')).join(' + ')}</span>
        {sum && <span className="num mt-1 block text-[13px] text-foreground">= <span className="font-semibold text-outcome-pass-fg">{sum}″</span></span>}
      </WordsBox>,
    );
  } else if (finding.arch_picture_id) {
    crops.push(<Crop key="a" src={pictureUrl(finding.arch_picture_id)} who={labels.architect} page={finding.arch_box?.page ?? finding.vs_page ?? finding.page} />);
  } else if (finding.source === 'rules' || finding.rule_check_id) {
    const should = shouldBeValue(finding);
    crops.push(<WordsBox key="r" who="GV rule">GV’s rule for this item{should && <>: <span className="num font-semibold text-outcome-pass-fg">{should.main}</span></>}</WordsBox>);
  }
  if (!crops.length) return null;
  return <span data-slot="chat-result-evidence" className="flex min-w-0 shrink-0 gap-2.5 max-md:w-full">{crops}</span>;
}

function Crop({ src, who, page }: { src: string; who: string; page: number }) {
  return (
    <span className="grid w-[168px] min-w-0 gap-1 max-md:w-auto max-md:flex-1">
      <span className="grid h-[116px] place-items-center overflow-hidden rounded-lg border bg-white max-md:h-[104px]">
        <img src={src} alt={`${who}, page ${page}`} loading="lazy" className="max-h-full max-w-full object-contain" />
      </span>
      <span className="flex min-w-0 justify-between gap-2 text-xs text-muted-foreground"><span className="truncate" title={who}>{who}</span><span className="num shrink-0">p.{page}</span></span>
    </span>
  );
}

function WordsBox({ who, children }: { who: string; children: React.ReactNode }) {
  return (
    <span className="grid w-[168px] min-w-0 gap-1 max-md:w-auto max-md:flex-1">
      <span className="grid h-[116px] content-center overflow-hidden rounded-lg border bg-muted/50 px-3 text-[12.5px] text-muted-foreground max-md:h-[104px]">{children}</span>
      <span className="text-xs text-muted-foreground">{who}</span>
    </span>
  );
}

function WrongRow({ finding, number, pictureUrl, onOpen }: { finding: ChatFinding; number: string | null; pictureUrl: (pictureId: string) => string; onOpen: (findingId: string) => void }) {
  const { thing } = itemParts(finding.item);
  return (
    <button
      type="button"
      data-slot="chat-result-row"
      data-kind="wrong"
      data-id={finding.id}
      aria-label={`${number ? `${number}: ` : ''}${thing}, page ${finding.page}. Open the evidence`}
      onClick={() => onOpen(finding.id)}
      className="grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-5 border-t px-[18px] py-4 text-left transition-colors first:border-t-0 hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset max-md:grid-cols-1 max-md:gap-3 max-md:px-3.5"
    >
      <span className="grid min-w-0 gap-1.5">
        <Where finding={finding} number={number} />
        <span className="text-[15px] leading-[22px] font-medium tracking-[-0.005em] text-foreground">{thing}</span>
        <ValuesLine finding={finding} />
      </span>
      <RowEvidence finding={finding} pictureUrl={pictureUrl} />
    </button>
  );
}

function CompactRow({ kind, finding, onOpen }: { kind: 'right' | 'rule_note' | 'not_compared' | 'dismissed'; finding: ChatFinding; onOpen: (findingId: string) => void }) {
  const { thing } = itemParts(finding.item);
  const vendor = vendorValue(finding);
  const architect = finding.architect ? finding.architect.replace(/"/g, '″').replace(/'/g, '′') : null;
  const note = kind === 'rule_note' ? ruleNoteWords(finding) : null;
  return (
    <button
      type="button"
      data-slot="chat-result-row"
      data-kind={kind}
      data-id={finding.id}
      aria-label={`${thing}, page ${finding.page}. Open the evidence`}
      onClick={() => onOpen(finding.id)}
      className="grid w-full min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3 border-t px-[18px] py-2.5 text-left transition-colors first:border-t-0 hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset max-md:grid-cols-[auto_minmax(0,1fr)] max-md:px-3.5"
    >
      <OutcomeGlyph kind={kind === 'dismissed' ? 'right' : kind} className="mt-0.5" />
      <span className="grid min-w-0 gap-0.5">
        <span className="text-sm leading-5 font-medium text-foreground">{thing}</span>
        <Where finding={finding} />
        {kind === 'not_compared' && <span className="text-[13px] text-muted-foreground">Needs <span className="font-medium text-foreground/80">{neededWords(finding).replace(/"/g, '″')}</span></span>}
        {note && <span data-slot="chat-result-rule" className="text-[13px] text-muted-foreground">{note.standard.replace(/"/g, '″')} · <span className="font-medium text-outcome-ask-fg">ask the architect</span></span>}
        {kind === 'dismissed' && <span className="text-[13px] text-muted-foreground">Right (reviewer){finding.decision?.by ? ` · marked by ${finding.decision.by}` : ''}{finding.decision?.note ? ` · “${finding.decision.note}”` : ''}</span>}
      </span>
      <span className="num text-right text-[13.5px] whitespace-nowrap text-foreground max-md:col-start-2 max-md:text-left">
        {vendor.main}
        {architect && <span className="block text-xs text-muted-foreground">architect {architect}</span>}
      </span>
    </button>
  );
}

// ── A later round (#1744) ─────────────────────────────────

/** The round strip: the only place for these three numbers. */
function RoundStrip({ rounds }: { rounds: RoundCounts }) {
  const cells: { kind: ResultTab; words: string; hint: string; count: number }[] = [
    { kind: 'right', words: ROUND_WORDS.closed, hint: 'Fixed by the vendor since the last round', count: rounds.fixed },
    { kind: 'wrong', words: ROUND_WORDS.notAddressed, hint: 'The vendor must fix these first', count: rounds.still_wrong },
    { kind: 'wrong', words: ROUND_WORDS.new, hint: 'Wrong for the first time in this round', count: rounds.new },
  ];
  return (
    <div data-slot="chat-result-rounds" aria-label="Since the last round" className="grid grid-cols-3 gap-2.5 max-md:gap-2">
      {cells.map((cell) => (
        <div key={cell.words} data-round-cell={cell.words} className="grid min-w-0 content-start gap-1 rounded-[14px] border bg-muted/30 px-4 py-3 max-md:px-2.5 max-md:py-2">
          <span className="flex min-w-0 items-start gap-2 text-[13px] leading-[18px] font-semibold max-md:text-xs"><OutcomeGlyph kind={cell.kind} className="mt-px size-4 [&_svg]:size-2.5" />{cell.words}</span>
          <span className="num text-[22px] leading-8 font-medium text-foreground max-md:text-xl">{cell.count}</span>
          <span className="text-[12.5px] leading-[18px] text-muted-foreground max-md:hidden">{cell.hint}</span>
        </div>
      ))}
    </div>
  );
}

function roundGroups(wrong: readonly ChatFinding[], right: readonly ChatFinding[]): { key: string; words: string; note?: string; findings: ChatFinding[] }[] {
  const notAddressed = wrong.filter((finding) => finding.round_status === 'still_wrong');
  const fresh = wrong.filter((finding) => finding.round_status !== 'still_wrong');
  const closed = right.filter((finding) => finding.round_status === 'fixed_in_later_round');
  return [
    { key: 'not_addressed', words: ROUND_WORDS.notAddressed, note: 'the vendor must fix these first', findings: notAddressed },
    { key: 'new', words: ROUND_WORDS.new, note: 'in this round', findings: fresh },
    { key: 'closed', words: ROUND_WORDS.closed, note: 'since the last round', findings: closed },
  ].filter((group) => group.findings.length > 0);
}

function SummaryFold({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div data-slot="chat-result-summary" className="grid min-w-0 gap-1">
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)} className="inline-flex w-fit items-center gap-1.5 rounded-md py-0.5 text-[13px] text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
        <ChevronRight aria-hidden="true" className={cn('size-3.5 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
        Summary as text
      </button>
      <Collapse open={open} id={id}>
        <div className="mt-1 border-l-2 pl-4 text-sm text-muted-foreground">{children}</div>
      </Collapse>
    </div>
  );
}
