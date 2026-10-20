import { useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, Download, ExternalLink, ImageOff, Minus, Plus, RotateCcw, X } from 'lucide-react';

import { StatusMark, type DecisionProps } from '@/components/chat/chat-findings';
import { Spinner } from '@/components/chat/chat-motion';
import { OutcomeGlyph } from '@/components/chat/chat-result';
import { RoundChip } from '@/components/chat/chat-trust';
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet';
import type { ChatFile, ChatFinding, FindingDecisionExtra, FindingDecisionValue } from '@/lib/chat-api';
import { architectPage, decisionHelpRows, pageWords, reviewerWords, sideLabels, vendorPage } from '@/lib/chat-findings';
import { proofGroup } from '@/lib/chat-proof';
import { boxOnPicture, itemParts, shouldBeValue, suggestedShouldBe, vendorValue } from '@/lib/chat-result';
import { plainChecks, sumWords, TRUST_WORDS, trustLevelOf } from '@/lib/chat-trust';
import { useReducedMotion } from '@/lib/chat-motion';
import { cn } from '@/lib/utils';

/**
 * Chat v2 (#1483): a finding's evidence, side by side and calm. The architect's crop and the
 * vendor's crop, large, each zoomable (buttons, wheel, double-click) and draggable when zoomed,
 * labelled with their page numbers; the finding's item, values and reason above. ← and → step
 * through the shown findings, Esc closes. A missing picture is a quiet "No evidence picture".
 * The crops are made by code from the PDF, not described by the AI.
 */

const MIN_ZOOM = 1;
const MAX_ZOOM = 6;
const ZOOM_STEP = 1.5;

const clampZoom = (value: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));

/** One crop, large, with zoom and pan. */
export function ZoomPane({ src, label, frameClassName }: { src: string; label: string; frameClassName?: string }) {
  const reduced = useReducedMotion();
  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; startX: number; startY: number } | null>(null);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [broken, setBroken] = useState(false);

  const zoomTo = (zoom: number) => setView((current) => {
    const next = clampZoom(zoom);
    // Back at full view the picture re-centres; otherwise the pan scales with the zoom.
    return next === 1 ? { zoom: 1, x: 0, y: 0 } : { zoom: next, x: (current.x * next) / current.zoom, y: (current.y * next) / current.zoom };
  });

  // The wheel zooms the picture rather than scrolling the dialog, so the listener cannot be passive.
  useEffect(() => {
    const node = frame.current;
    if (!node) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setView((current) => {
        const next = clampZoom(current.zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15));
        return next === 1 ? { zoom: 1, x: 0, y: 0 } : { zoom: next, x: (current.x * next) / current.zoom, y: (current.y * next) / current.zoom };
      });
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  }, []);

  if (broken) return <NoPicture />;

  return (
    <div className="grid min-h-0 gap-1.5">
      <div
        ref={frame}
        data-slot="chat-evidence-frame"
        data-zoom={view.zoom.toFixed(2)}
        onDoubleClick={() => zoomTo(view.zoom > 1 ? 1 : 2.5)}
        onPointerDown={(event) => {
          if (view.zoom <= 1) return;
          drag.current = { x: event.clientX, y: event.clientY, startX: view.x, startY: view.y };
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={(event) => {
          const start = drag.current;
          if (!start) return;
          setView((current) => ({ ...current, x: start.startX + event.clientX - start.x, y: start.startY + event.clientY - start.y }));
        }}
        onPointerUp={() => { drag.current = null; }}
        onPointerCancel={() => { drag.current = null; }}
        className={cn(
          'relative grid place-items-center overflow-hidden rounded-lg border bg-white touch-none select-none',
          frameClassName ?? 'h-[min(56vh,520px)] max-md:h-[34vh]',
          view.zoom > 1 ? 'cursor-grab active:cursor-grabbing' : 'cursor-zoom-in',
        )}
      >
        <img
          src={src}
          alt={label}
          draggable={false}
          onError={() => setBroken(true)}
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}
          className={cn('max-h-full max-w-full object-contain', !reduced && 'transition-transform duration-150 ease-out')}
        />
      </div>
      <div className="flex items-center justify-end gap-0.5" role="group" aria-label={`Zoom: ${label}`}>
        <ZoomButton label={`Zoom out: ${label}`} onClick={() => zoomTo(view.zoom / ZOOM_STEP)} disabled={view.zoom <= MIN_ZOOM}><Minus className="size-3.5" aria-hidden="true" /></ZoomButton>
        <span className="num w-11 text-center text-xs text-muted-foreground" aria-live="polite">{Math.round(view.zoom * 100)}%</span>
        <ZoomButton label={`Zoom in: ${label}`} onClick={() => zoomTo(view.zoom * ZOOM_STEP)} disabled={view.zoom >= MAX_ZOOM}><Plus className="size-3.5" aria-hidden="true" /></ZoomButton>
        <ZoomButton label={`Fit: ${label}`} onClick={() => zoomTo(1)} disabled={view.zoom === 1}><RotateCcw className="size-3.5" aria-hidden="true" /></ZoomButton>
      </div>
    </div>
  );
}

function ZoomButton({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled: boolean; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label.split(':')[0]}
      onClick={onClick}
      disabled={disabled}
      className="grid size-7 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-40 motion-reduce:transition-none"
    >
      {children}
    </button>
  );
}

export function NoPicture({ compact = false }: { compact?: boolean }) {
  return (
    <div
      data-slot="chat-evidence-missing"
      className={cn('grid place-items-center rounded-lg border border-dashed text-sm text-muted-foreground', compact ? 'h-[190px] max-md:h-[140px]' : 'h-[min(56vh,520px)] max-md:h-[34vh]')}
    >
      <span className="inline-flex items-center gap-2">
        <ImageOff aria-hidden="true" className="size-4" />
        No evidence picture
      </span>
    </div>
  );
}

/** The viewer's words: what each side shows, why, and what to tell the vendor (the card's words). */
function ViewerWords({ finding }: { finding: ChatFinding }) {
  const words = reviewerWords(finding);
  const help = decisionHelpRows(finding);
  return (
    <>
      <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
        <div className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{words.referenceLabel}</dt><dd className="break-words">{words.reference}</dd></div>
        <div className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{words.vendorLabel}</dt><dd className="break-words">{words.vendor}</dd></div>
      </dl>
      <SheetDescription className="break-words text-foreground">
        <span className="text-muted-foreground">{words.whyLabel}: </span>{words.why}
      </SheetDescription>
      {help.length > 0 && (
        <dl data-slot="chat-evidence-decision-help" className="grid gap-1 text-sm">
          {help.map((row) => (
            <div key={row.label} className="min-w-0"><dt className="inline text-muted-foreground">{row.label}: </dt><dd className="inline break-words">{row.text}</dd></div>
          ))}
        </dl>
      )}
      {words.tell && <p data-slot="chat-evidence-tell" className="text-sm font-medium break-words">{words.tell}</p>}
      <p className="text-xs text-muted-foreground">How sure: {trustLevelOf(finding) ? `${TRUST_WORDS[trustLevelOf(finding)!]}. ` : ''}{words.sure}</p>
    </>
  );
}

export interface EvidenceViewerProps {
  /** The findings the arrows step through, in the result's order. */
  findings: readonly ChatFinding[];
  /** The finding shown, by id; null when the viewer is closed. */
  openId: string | null;
  onOpen: (findingId: string | null) => void;
  pictureUrl: (pictureId: string) => string;
  /** Where "Open in review screen" goes; the link is left out when there is none. */
  fullPageHref?: string | null;
  /** #1717: the vendor's page picture (100 dpi, the set pack's), to show the spot boxed; absent when there is none. */
  pagePicture?: ((page: number) => string) | null;
  /** #1717: the cloud drawing at a page ("Open on the cloud drawing"); absent when there is none. */
  cloudAt?: ((page: number) => string) | null;
  /** #1717 (owner decision 3): the optional override lives here, not on the rows. */
  decisionFor?: ((finding: ChatFinding) => DecisionProps) | null;
  /** #1717: the comment number the letter gives a Wrong item (C-04). */
  numberOf?: ((finding: ChatFinding) => string | null) | null;
  /** #1717: why the override was not saved, said in the panel (the page behind it is hidden while it is open). */
  notice?: string | null;
  /** #1803: the draft made again after each decision: its state, and the updated package to download. */
  draft?: DraftState | null;
}

/** #1803: the draft (cloud drawing, letter, register, Not-compared list, package) after the reviewer's decisions. */
export interface DraftState {
  updating: boolean;
  failed: boolean;
  /** "14:32" once made again; null before. */
  updatedAt: string | null | undefined;
  pkg: ChatFile | null;
  href: string | null;
  onRetry: (() => void) | null;
}

/**
 * #1717: the evidence panel, a sheet from the right (full screen on a phone): the two values large,
 * both crops (zoomable), the spot boxed on the vendor's page where its picture exists, why (in
 * plain words), the comment for the vendor, and the optional override. J / K or ← / → step through
 * the result's items, Esc closes and focus goes back to the row.
 */
export function EvidenceViewer({ findings, openId, onOpen, pictureUrl, fullPageHref, pagePicture = null, cloudAt = null, decisionFor = null, numberOf = null, notice = null, draft = null }: EvidenceViewerProps) {
  const at = openId === null ? -1 : findings.findIndex((finding) => finding.id === openId);
  const finding = at >= 0 ? findings[at] : null;
  const previous = at > 0 ? findings[at - 1] : null;
  const next = at >= 0 && at < findings.length - 1 ? findings[at + 1] : null;
  const decision = finding && decisionFor ? decisionFor(finding) : null;
  const laid = decision ? (decision.saved ?? (decision.decision ? { decision: decision.decision } : null)) : finding?.decision ?? null;
  const group = finding ? proofGroup(finding, laid) : null;
  const number = finding && numberOf ? numberOf(finding) : null;
  const parts = finding ? itemParts(finding.item) : null;
  const vendor = finding ? vendorValue(finding) : null;
  const should = finding && group === 'clouded' ? shouldBeValue(finding, laid) : null;
  // J / K and the arrows step while the panel is open, wherever focus is (a button that becomes
  // disabled at the end of the list drops focus out of the panel).
  const previousId = previous?.id ?? null;
  const nextId = next?.id ?? null;
  const isOpen = finding !== null;
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest?.('input, textarea, [contenteditable="true"]')) return;
      if ((event.key === 'ArrowLeft' || event.key === 'k') && previousId) { event.preventDefault(); onOpen(previousId); }
      if ((event.key === 'ArrowRight' || event.key === 'j') && nextId) { event.preventDefault(); onOpen(nextId); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, previousId, nextId, onOpen]);

  return (
    <Sheet open={finding !== null} onOpenChange={(open) => { if (!open) onOpen(null); }}>
      {finding && parts && vendor && (
        <SheetContent
          side="right"
          showCloseButton={false}
          data-slot="chat-evidence-viewer"
          // The panel opens outside the chat page (a portal): it takes the page's own face and accent.
          data-tw
          className="chat-sheet chat-brand font-sans text-sm text-foreground antialiased w-full gap-0 p-0 data-[state=closed]:duration-200 data-[state=open]:duration-300 sm:max-w-[600px]"
        >
          <div className="flex min-w-0 items-center gap-2 border-b px-5 py-3 max-md:px-4">
            {number && <span data-slot="chat-evidence-number" className="num inline-flex h-[22px] items-center rounded-full border border-outcome-fail-fg/25 bg-outcome-fail-bg px-2 text-xs font-semibold text-outcome-fail-fg">{number}</span>}
            <StatusMark finding={finding} />
            <RoundChip finding={finding} />
            <span className="flex-1" />
            <span data-slot="chat-evidence-position" className="px-1 text-xs text-muted-foreground"><span className="num">{at + 1}</span> of <span className="num">{findings.length}</span> {findings.length === 1 ? 'item' : 'items'}</span>
            <button type="button" aria-label="Previous finding" disabled={!previous} onClick={() => previous && onOpen(previous.id)} className={ICON_BUTTON}><ChevronLeft className="size-4" aria-hidden="true" /></button>
            <button type="button" aria-label="Next finding" disabled={!next} onClick={() => next && onOpen(next.id)} className={ICON_BUTTON}><ChevronRight className="size-4" aria-hidden="true" /></button>
            <SheetClose aria-label="Close" className={ICON_BUTTON}><X className="size-4" aria-hidden="true" /></SheetClose>
          </div>

          <div className="grid min-h-0 flex-1 content-start gap-5 overflow-y-auto px-5 py-5 max-md:px-4">
            <div className="grid min-w-0 gap-1">
              <p aria-hidden="true" className="flex flex-wrap gap-x-2 text-[12.5px] text-muted-foreground">{parts.area && <span>{parts.area}</span>}<span className="num">p.{pageWords(finding)}</span></p>
              <SheetTitle className="text-[19px] leading-[26px] font-semibold tracking-[-0.01em] break-words">
                {parts.area ? parts.thing : finding.item}
                <span className="sr-only"> · page {pageWords(finding)}</span>
              </SheetTitle>
            </div>

            <div className="grid min-w-0 grid-cols-2 gap-3 max-md:gap-2">
              <ValueCard side="vendor" finding={finding} label={sideLabels(finding).vendor} value={vendor.main} tone={group === 'clouded' ? 'wrong' : null} note={vendor.note ? `Printed ${finding.vendor}` : null} src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} />
              <ValueCard side="architect" finding={finding} label={should ? 'Should be' : sideLabels(finding).architect} value={should ? should.main : finding.architect ? finding.architect.replace(/"/g, '″').replace(/'/g, '′') : '—'} tone={should ? 'right' : null} note={should?.note ? `From ${should.note}` : null} src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} />
            </div>

            {pagePicture && (finding.vendor_box || finding.arch_box) && <PageSpot finding={finding} src={pagePicture((finding.vendor_box ?? finding.arch_box)!.page)} />}

            <section aria-label="Why" className="grid min-w-0 gap-2">
              <h3 className="text-xs font-semibold text-muted-foreground">{group === 'clouded' ? 'Why it is Wrong' : group === 'right' ? 'Why it is Right' : group === 'dismissed' ? 'What code found' : 'Why it was not compared'}</h3>
              <ViewerWords finding={finding} />
              {sumWords(finding) && (
                <p data-slot="chat-evidence-sum" className="text-sm tabular-nums">
                  <span className="text-muted-foreground">Code’s sum: </span>
                  {sumWords(finding)}
                </p>
              )}
              {plainChecks(finding).length > 0 && (
                <ul data-slot="chat-evidence-checks" className="grid gap-1.5 text-[13.5px]">
                  {plainChecks(finding).map((line) => <li key={line} className="flex gap-2"><Check className="mt-0.5 size-4 shrink-0" aria-hidden="true" />{line}</li>)}
                </ul>
              )}
            </section>
          </div>

          <div className="grid min-w-0 gap-3 border-t px-5 py-3 max-md:px-4">
            {notice && <p role="alert" data-slot="chat-evidence-notice" className="text-[13px] text-muted-foreground">{notice}</p>}
            {decision && group && group !== 'right' && (
              <ReviewerCall key={finding.id} finding={finding} group={group} decision={decision} />
            )}
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              {draft ? <DraftLine draft={draft} /> : <span className="text-xs text-muted-foreground max-md:hidden"><kbd className="num rounded border px-1">J</kbd> <kbd className="num rounded border px-1">K</kbd> to step · <kbd className="num rounded border px-1">Esc</kbd></span>}
              <span className="flex-1" />
              {cloudAt && group === 'clouded' && (
                <a href={cloudAt(finding.vendor_box?.page ?? finding.page)} target="_blank" rel="noopener noreferrer" className={SMALL_BUTTON}><ExternalLink className="size-3.5" aria-hidden="true" />Open on the cloud drawing</a>
              )}
              {fullPageHref && (
                <a href={fullPageHref} className={SMALL_BUTTON}><ExternalLink className="size-3.5" aria-hidden="true" />Open in review screen</a>
              )}
            </div>
          </div>
        </SheetContent>
      )}
    </Sheet>
  );
}

// ── #1803: the reviewer's own call on the item ──────────────

type CallMode = 'choose' | 'wrong' | 'right' | 'remove';

/** The lines under each choice: what happens, in plain words. */
const CALL_WORDS = {
  wrong: 'You checked this yourself. It goes on the cloud drawing and the letter, marked “(reviewer)”.',
  right: 'You checked this yourself. It leaves the Not-compared list and shows as Right (reviewer).',
  remove: 'The red cloud and its comment come off. It shows as Right (reviewer) in the vendor’s files.',
} as const;

/**
 * #1803: what the reviewer can decide here, each choice with one line saying what happens. A
 * Not-compared item: "It's a mistake: add a red mark" (asks what it should be) or "It's fine: mark
 * Right". A Wrong item: "Not a mistake: remove the red mark". Nothing is saved until the small form's
 * own button; the note goes to the vendor.
 */
function ReviewerCall({ finding, group, decision }: { finding: ChatFinding; group: string; decision: DecisionProps }) {
  const [mode, setMode] = useState<CallMode>('choose');
  const saved = decision.saved ?? null;
  const decide = (value: FindingDecisionValue, extra: FindingDecisionExtra) => { decision.onDecide(value, extra); setMode('choose'); };
  if (mode === 'wrong') return <CallForm kind="wrong" finding={finding} busy={decision.busy} onCancel={() => setMode('choose')} onSave={(extra) => decide('agree', extra)} />;
  if (mode === 'right') return <CallForm kind="right" finding={finding} busy={decision.busy} onCancel={() => setMode('choose')} onSave={(extra) => decide('not_a_problem', extra)} />;
  if (mode === 'remove') return <CallForm kind="remove" finding={finding} busy={decision.busy} onCancel={() => setMode('choose')} onSave={(extra) => decide('not_a_problem', extra)} />;
  const mine = saved?.decision === 'agree' && group === 'clouded' ? 'wrong' : saved?.decision === 'not_a_problem' && group === 'dismissed' ? 'right' : null;
  return (
    <section aria-label="Your call" data-slot="chat-evidence-call" data-group={group} className="grid min-w-0 gap-2">
      <p className="text-xs font-medium text-muted-foreground">{mine ? 'Your call' : 'Your call · optional'}</p>
      {mine && (
        <p data-slot="chat-evidence-call-saved" className="flex min-w-0 items-start gap-2 text-[13px]">
          <OutcomeGlyph kind={mine} className="mt-px size-4 [&_svg]:size-2.5" />
          <span className="min-w-0 break-words">
            <span className="font-medium">{mine === 'wrong' ? 'Wrong (reviewer).' : 'Right (reviewer).'}</span>{' '}
            <span className="text-muted-foreground">{mine === 'wrong' ? 'You added this red mark.' : 'You marked this Right.'}{saved?.note ? <> Note: “{saved.note}”</> : null}</span>
          </span>
        </p>
      )}
      <div className="grid min-w-0 gap-2 sm:grid-cols-2">
        {group === 'clouded' && <CallChoice kind="remove" onClick={() => setMode('remove')} busy={decision.busy} />}
        {(group === 'not_compared' || group === 'dismissed') && <CallChoice kind="wrong" onClick={() => setMode('wrong')} busy={decision.busy} />}
        {group === 'not_compared' && <CallChoice kind="right" onClick={() => setMode('right')} busy={decision.busy} />}
      </div>
    </section>
  );
}

const CHOICE_WORDS = {
  wrong: 'It’s a mistake: add a red mark',
  right: 'It’s fine: mark Right',
  remove: 'Not a mistake: remove the red mark',
} as const;

function CallChoice({ kind, onClick, busy }: { kind: 'wrong' | 'right' | 'remove'; onClick: () => void; busy?: boolean }) {
  const hint = useId();
  return (
    <div className="grid min-w-0 content-start gap-1">
      <button type="button" data-slot={`chat-evidence-call-${kind}`} aria-describedby={hint} disabled={busy} onClick={onClick} className={cn(SMALL_BUTTON, 'w-full justify-start text-left')}>
        {kind === 'remove' ? <X className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" /> : <OutcomeGlyph kind={kind} className="size-4 [&_svg]:size-2.5" />}
        <span className="min-w-0 truncate">{CHOICE_WORDS[kind]}</span>
      </button>
      <p id={hint} className="text-xs leading-[17px] text-muted-foreground">{CALL_WORDS[kind]}</p>
    </div>
  );
}

/** The small form behind a choice: what it should be (a red mark only), a note, and its own Save. */
function CallForm({ kind, finding, busy, onCancel, onSave }: { kind: 'wrong' | 'right' | 'remove'; finding: ChatFinding; busy?: boolean; onCancel: () => void; onSave: (extra: FindingDecisionExtra) => void }) {
  const suggested = kind === 'wrong' ? suggestedShouldBe(finding) : '';
  const [value, setValue] = useState(suggested);
  const [note, setNote] = useState('');
  const valueId = useId();
  const noteId = useId();
  const hint = useId();
  const missing = kind === 'wrong' && !value.trim();
  const save = kind === 'wrong' ? 'Add the red mark' : kind === 'right' ? 'Mark Right' : 'Remove the red mark';
  return (
    <form
      aria-label={CHOICE_WORDS[kind]}
      data-slot={`chat-evidence-form-${kind}`}
      className="grid min-w-0 gap-2.5"
      onSubmit={(event) => { event.preventDefault(); if (!missing) onSave(kind === 'wrong' ? { should_be: value.trim(), note } : { note }); }}
      onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); event.preventDefault(); onCancel(); } }}
    >
      <p className="flex items-center gap-2 text-[13.5px] font-medium">
        {kind === 'remove' ? <X className="size-3.5 text-muted-foreground" aria-hidden="true" /> : <OutcomeGlyph kind={kind} className="size-4 [&_svg]:size-2.5" />}
        {CHOICE_WORDS[kind]}
      </p>
      <div className={cn('grid min-w-0 gap-2.5', kind === 'wrong' && 'sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]')}>
        {kind === 'wrong' && (
          <label htmlFor={valueId} className="grid min-w-0 gap-1">
            <span className="text-xs text-muted-foreground">Should be</span>
            <input id={valueId} autoFocus value={value} onChange={(event) => setValue(event.target.value)} maxLength={80} placeholder='e.g. 34 1/2"' aria-describedby={hint} aria-invalid={missing || undefined} className={INPUT + ' num'} />
          </label>
        )}
        <label htmlFor={noteId} className="grid min-w-0 gap-1">
          <span className="text-xs text-muted-foreground">{kind === 'remove' ? 'Reason (optional, the vendor reads it)' : 'Note (optional, the vendor reads it)'}</span>
          <input id={noteId} autoFocus={kind !== 'wrong'} value={note} onChange={(event) => setNote(event.target.value)} maxLength={300} className={INPUT} />
        </label>
      </div>
      <p id={hint} className="text-xs leading-[17px] text-muted-foreground">
        {kind === 'wrong' && (suggested ? 'Filled in with the architect’s value; change it if it should be something else. ' : 'The architect gives no value here: type what it should be. ')}
        {CALL_WORDS[kind]}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <button type="submit" data-slot="chat-evidence-form-save" disabled={busy || missing} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-primary px-3 text-[13px] font-medium text-primary-foreground hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:opacity-50">{save}</button>
        <button type="button" onClick={onCancel} className={SMALL_BUTTON}>Cancel</button>
      </div>
    </form>
  );
}

/** #1803: the draft made again after a decision: "Updating the draft…", then "Draft updated 14:32" and its download. */
function DraftLine({ draft }: { draft: DraftState }) {
  if (draft.updating) {
    return <p role="status" data-slot="chat-evidence-draft" data-state="updating" className="inline-flex items-center gap-2 text-xs text-muted-foreground"><Spinner className="size-3.5" />Updating the draft…</p>;
  }
  if (draft.failed) {
    return (
      <p role="alert" data-slot="chat-evidence-draft" data-state="failed" className="inline-flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        The draft could not be updated.
        {draft.onRetry && <button type="button" onClick={draft.onRetry} className="rounded font-medium text-foreground underline underline-offset-2 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">Try again</button>}
      </p>
    );
  }
  if (draft.updatedAt) {
    return (
      <p role="status" data-slot="chat-evidence-draft" data-state="updated" className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
        <span>Draft updated <span className="num">{draft.updatedAt}</span></span>
        {draft.pkg && draft.href && <a href={draft.href} download={draft.pkg.name} className="inline-flex items-center gap-1 rounded font-medium text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"><Download className="size-3.5" aria-hidden="true" />Download updated draft</a>}
      </p>
    );
  }
  return <span className="text-xs text-muted-foreground max-md:hidden"><kbd className="num rounded border px-1">J</kbd> <kbd className="num rounded border px-1">K</kbd> to step · <kbd className="num rounded border px-1">Esc</kbd></span>;
}

const INPUT = 'h-8 w-full min-w-0 rounded-lg border bg-background px-2.5 text-[13px] text-foreground placeholder:text-muted-foreground/70 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-invalid:border-outcome-fail-fg/50';

const ICON_BUTTON = 'grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-40';
const SMALL_BUTTON = 'inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-2.5 text-[13px] font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60 aria-pressed:bg-muted';

/** One side: its value large, then its crop (zoomable). */
function ValueCard({ side, finding, label, value, tone, note, src }: { side: 'vendor' | 'architect'; finding: ChatFinding; label: string; value: string; tone: 'wrong' | 'right' | null; note: string | null; src: string | null }) {
  const page = side === 'vendor' ? vendorPage(finding) : architectPage(finding);
  return (
    <figure data-slot="chat-evidence-side" data-side={side} className="grid min-w-0 content-start overflow-hidden rounded-[10px] border">
      <figcaption className="flex min-w-0 items-baseline justify-between gap-2 border-b px-3 py-2">
        <span className="text-[12.5px] text-muted-foreground">{label}</span>
        <span className={cn('num truncate text-[20px] leading-[26px] font-semibold max-md:text-[17px]', tone === 'wrong' && 'text-outcome-fail-fg', tone === 'right' && 'text-outcome-pass-fg')}>{value}</span>
      </figcaption>
      <div className="p-2">{src ? <ZoomPane key={src} src={src} label={`${sideLabels(finding)[side]}, page ${page}`} frameClassName="h-[190px] max-md:h-[140px]" /> : <NoPicture compact />}</div>
      <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">{note ?? <>Page <span className="num">{page}</span></>}</p>
    </figure>
  );
}

/** The vendor's page with the value boxed: zoomed to the spot, or the whole page. */
function PageSpot({ finding, src }: { finding: ChatFinding; src: string }) {
  const [mode, setMode] = useState<'vendor' | 'architect' | 'page'>(finding.vendor_box ? 'vendor' : 'architect');
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [broken, setBroken] = useState(false);
  const frame = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const [view, setView] = useState(556);
  // The frame's width, measured after it is on screen (never read from the ref while drawing).
  useEffect(() => {
    const node = frame.current;
    if (!node) return;
    const measure = () => setView(node.clientWidth || 556);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, [broken]);
  const sameFile = finding.vendor_box && finding.arch_box && finding.vendor_box.file === finding.arch_box.file && finding.vendor_box.page === finding.arch_box.page;
  const boxes = { vendor: boxOnPicture(finding.vendor_box), architect: sameFile || !finding.vendor_box ? boxOnPicture(finding.arch_box) : null };
  if (broken) return null;
  const height = 280;
  const scale = mode === 'page' && size ? view / size.width : 1;
  const focus = mode === 'page' ? null : boxes[mode] ?? boxes.vendor ?? boxes.architect;
  const x = focus ? view / 2 - (focus.left + focus.width / 2) * scale : 0;
  const y = focus ? height / 2 - (focus.top + focus.height / 2) * scale : 0;
  const motion = reduced ? undefined : 'transform 320ms cubic-bezier(0.2,0,0,1), width 320ms cubic-bezier(0.2,0,0,1)';
  return (
    <section aria-label="On the drawing" data-slot="chat-evidence-page" className="grid min-w-0 gap-2">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-muted-foreground">On the drawing · <span className="num">p.{(finding.vendor_box ?? finding.arch_box)?.page}</span></h3>
        <div role="group" aria-label="Show" className="inline-flex rounded-lg bg-muted p-0.5">
          {(['vendor', 'architect', 'page'] as const).filter((each) => each === 'page' || boxes[each]).map((each) => (
            <button key={each} type="button" aria-pressed={mode === each} onClick={() => setMode(each)} className={cn('h-7 rounded-md px-2.5 text-xs font-medium', mode === each ? 'bg-card text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.08)]' : 'text-muted-foreground hover:text-foreground')}>
              {each === 'vendor' ? 'Vendor spot' : each === 'architect' ? 'Architect spot' : 'Whole page'}
            </button>
          ))}
        </div>
      </div>
      <div ref={frame} className="relative h-[280px] overflow-hidden rounded-[10px] border bg-white">
        <div style={{ position: 'absolute', left: 0, top: 0, transform: `translate(${x}px, ${y}px) scale(${scale})`, transformOrigin: '0 0', transition: motion }}>
          <img src={src} alt={`Vendor page ${(finding.vendor_box ?? finding.arch_box)?.page}`} onError={() => setBroken(true)} onLoad={(event) => setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} className="block max-w-none" />
          {(['vendor', 'architect'] as const).map((each) => boxes[each] && (
            <span key={each} aria-hidden="true" className={cn('absolute rounded-[3px] border-2 shadow-[0_0_0_3px_rgb(255_255_255/0.9)]', each === 'vendor' ? 'border-outcome-fail' : 'border-outcome-pass')} style={{ left: boxes[each]!.left - 4, top: boxes[each]!.top - 4, width: boxes[each]!.width + 8, height: boxes[each]!.height + 8 }} />
          ))}
        </div>
      </div>
    </section>
  );
}
