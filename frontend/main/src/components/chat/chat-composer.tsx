import { useId, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUp, Check, ChevronDown, Square } from 'lucide-react';
import { DropdownMenu as MenuPrimitive } from 'radix-ui';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { ChatEffort, ChatModel } from '@/lib/chat-api';
import { blankSegments, firstBlank, nextBlank } from '@/lib/chat-plus-actions';
import {
  alwaysThinksWords,
  chooseEffort,
  chooseModel,
  chooseThinking,
  EFFORT_WORDS,
  EFFORTS,
  effortBlockedReason,
  modelBlurb,
  shortModelLabel,
  type ChatChoice,
} from '@/lib/chat-settings';
import { cn } from '@/lib/utils';

/** The most a message may hold (the server refuses more). */
export const MESSAGE_MAX = 8000;
/** The box grows with its words up to this share of the window's height, then scrolls. */
const MAX_HEIGHT_SHARE = 0.4;

/** A row in the model menu: claude.ai's roomy two-line rows, a check on the right when picked. */
const ROW =
  'relative flex w-full cursor-default items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm outline-hidden select-none focus:bg-accent data-[disabled]:cursor-default data-[disabled]:opacity-60';

export interface ChatComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  /** A message is running: Send becomes Stop. */
  running: boolean;
  /** Stop was pressed and the server is finishing. */
  stopping: boolean;
  /** Why sending is off now, in plain words; null when it is on. */
  blocked: string | null;
  models: readonly ChatModel[];
  /** The current choice; null until the models have loaded. */
  choice: ChatChoice | null;
  onChoice: (choice: ChatChoice) => void;
  placeholder: string;
  /** Small muted words at the foot of the model menu (what a message may cost). */
  menuFootnote?: string;
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
  /** Shown at the bottom left (chat v4: the Countertop / Cabinet chips). */
  leading?: React.ReactNode;
  /** The "+" menu (#1517), first at the bottom left. */
  plus?: React.ReactNode;
  /**
   * #1712 (owner decision 4): the model is chosen in the "+" menu, so the main row shows no model
   * button. Without it (a composer with no "+" menu) the model button stays.
   */
  modelInMenu?: boolean;
}

/**
 * The composer, like claude.ai's (#1471): a rounded card whose box grows with the words (Enter sends,
 * Shift+Enter starts a new line), and on its bottom right one model button and Send, which becomes
 * Stop while a message runs. The model button's menu holds everything else: the models, Extended
 * thinking and Effort. Opus 5.5 always thinks, so its switch is locked on and says so; with
 * thinking off, the efforts above High cannot be picked and say why.
 */
export function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  running,
  stopping,
  blocked,
  models,
  choice,
  onChoice,
  placeholder,
  menuFootnote,
  textareaRef,
  leading,
  plus,
  modelInMenu = false,
}: ChatComposerProps) {
  const fieldId = useId();
  const ownRef = useRef<HTMLTextAreaElement>(null);
  const ref = textareaRef ?? ownRef;
  const model = choice ? models.find((candidate) => candidate.id === choice.model) ?? null : null;
  // A prompt from the "+" menu may hold a blank ("[page number]"): Send waits until it is filled.
  const blank = firstBlank(value);
  const canSend = !running && !blocked && !blank && value.trim().length > 0;
  const highlight = useRef<HTMLDivElement>(null);

  // The box follows its words, also when they change from outside (sent, or given back after a refusal).
  useLayoutEffect(() => {
    const box = ref.current;
    if (!box) return;
    box.style.height = 'auto';
    const most = typeof window === 'undefined' ? 320 : window.innerHeight * MAX_HEIGHT_SHARE;
    box.style.height = `${Math.min(box.scrollHeight, most)}px`;
    box.style.overflowY = box.scrollHeight > most ? 'auto' : 'hidden';
  }, [value, ref]);

  return (
    <form
      data-slot="chat-composer"
      data-model={choice?.model ?? undefined}
      aria-label="Ask GV Review"
      className="flex flex-col rounded-[18px] border border-border bg-background shadow-[0_4px_20px_rgb(0_0_0/0.04),0_1px_2px_rgb(0_0_0/0.04)] transition-[border-color,box-shadow] duration-150 focus-within:border-input/70 focus-within:shadow-[0_4px_20px_rgb(0_0_0/0.06),0_0_0_3px_color-mix(in_srgb,var(--ring)_8%,transparent)] dark:shadow-[0_4px_20px_rgb(0_0_0/0.3)]"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSend) onSend();
      }}
    >
      <label htmlFor={fieldId} className="sr-only">
        Your message
      </label>
      <div className="relative">
      {blank && (
        // The blanks, highlighted behind the (transparent) box: the same words, laid out the same way.
        <div
          ref={highlight}
          aria-hidden="true"
          data-slot="chat-composer-blanks"
          className="pointer-events-none absolute inset-0 overflow-hidden px-4 pt-3.5 pb-1 text-[15px] leading-[1.55] break-words whitespace-pre-wrap text-transparent"
        >
          {blankSegments(value).map((segment, at) =>
            segment.blank ? (
              <mark key={at} className="rounded-[4px] bg-[color-mix(in_srgb,var(--outcome-review)_24%,transparent)] text-transparent">{segment.text}</mark>
            ) : (
              <span key={at}>{segment.text}</span>
            ),
          )}
        </div>
      )}
      <textarea
        id={fieldId}
        ref={ref}
        rows={1}
        value={value}
        maxLength={MESSAGE_MAX}
        placeholder={placeholder}
        aria-describedby={blank ? `${fieldId}-blank` : undefined}
        onChange={(event) => onChange(event.target.value)}
        onScroll={(event) => {
          if (highlight.current) highlight.current.scrollTop = event.currentTarget.scrollTop;
        }}
        onKeyDown={(event) => {
          const box = event.currentTarget;
          // Tab (Shift+Tab) moves between the blanks while any are left; otherwise it leaves the box.
          if (event.key === 'Tab' && !event.altKey && !event.metaKey && !event.ctrlKey && blank) {
            const to = nextBlank(value, event.shiftKey ? box.selectionStart : box.selectionEnd, event.shiftKey);
            if (to) {
              event.preventDefault();
              box.setSelectionRange(to[0], to[1]);
            }
            return;
          }
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (canSend) onSend();
            else if (blank) {
              // Enter on a prompt with a blank picks the blank, ready to type over.
              const to = nextBlank(value, 0);
              if (to) box.setSelectionRange(to[0], to[1]);
            }
          }
        }}
        className="relative block max-h-[40vh] min-h-[52px] w-full resize-none border-0 bg-transparent px-4 pt-3.5 pb-1 text-[15px] leading-[1.55] text-foreground outline-none placeholder:text-muted-foreground"
      />
      </div>
      {blank && (
        <p id={`${fieldId}-blank`} data-slot="chat-composer-blank-hint" className="px-4 pt-0.5 text-xs text-muted-foreground">
          Fill in {blank} to send · <kbd className="font-sans">Tab</kbd> moves to the next blank
        </p>
      )}
      {/* #1685: the row wraps at phone width, so the product chips never run under the model menu;
          the model menu and Send stay together at the end of whichever line they land on. */}
      <div data-slot="chat-composer-tools" className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-1.5 px-2.5 pt-1 pb-2.5">
        {plus}
        {leading}
        <div data-slot="chat-composer-end" className="ml-auto flex shrink-0 items-center gap-1">
          {value.length > MESSAGE_MAX - 500 && (
            <span className="mr-1 text-xs text-muted-foreground">
              <span className="num">{value.length}</span>/<span className="num">{MESSAGE_MAX}</span>
            </span>
          )}
          {model && choice && !modelInMenu && <ModelMenu models={models} model={model} choice={choice} onChoice={onChoice} footnote={menuFootnote} />}
          {running ? (
            <button
              type="button"
              onClick={onStop}
              disabled={stopping}
              aria-label="Stop response"
              title={stopping ? 'Stopping…' : 'Stop response'}
              className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground transition-[opacity,transform] duration-150 hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none active:scale-95 disabled:opacity-50 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:zoom-in-90"
            >
              <Square className="size-3 fill-current" aria-hidden="true" />
            </button>
          ) : (
            <button
              type="submit"
              aria-label="Send message"
              title={blocked ?? (blank ? `Fill in ${blank} first` : 'Send message')}
              disabled={!canSend}
              className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground transition-[opacity,transform,background-color] duration-150 hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none active:scale-95 disabled:cursor-default disabled:bg-muted-foreground/40 disabled:opacity-60 disabled:active:scale-100"
            >
              <ArrowUp className="size-4" aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

/**
 * The model button and its menu, in claude.ai's style: it opens upward with a quick fade and scale;
 * arrows move, Enter picks, Esc closes, focus stays inside while it is open and a click outside
 * closes it (all from the menu primitive).
 */
function ModelMenu({
  models,
  model,
  choice,
  onChoice,
  footnote,
}: {
  models: readonly ChatModel[];
  model: ChatModel;
  choice: ChatChoice;
  onChoice: (choice: ChatChoice) => void;
  footnote?: string;
}) {
  const locked = model.thinking === 'always';
  // Effort opens in place under its row (a side submenu has no room on a phone).
  const [effortOpen, setEffortOpen] = useState(false);
  const effortsId = useId();
  return (
    <DropdownMenu onOpenChange={(open) => !open && setEffortOpen(false)}>
      <DropdownMenuTrigger
        aria-label={`Model: ${model.label}`}
        className="inline-flex h-8 shrink-0 items-center gap-1 rounded-lg px-2 text-[13.5px] text-foreground/80 transition-colors duration-150 hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none data-[state=open]:bg-accent data-[state=open]:text-foreground"
      >
        <span className="max-w-[9rem] truncate">{shortModelLabel(model)}</span>
        <ChevronDown className="size-3.5 opacity-70 transition-transform duration-150 in-data-[state=open]:rotate-180" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side="top"
        align="end"
        sideOffset={8}
        aria-label="Model and thinking"
        collisionPadding={12}
        className="w-[18rem] max-w-[calc(100vw-24px)] rounded-xl p-1.5 font-sans shadow-lg duration-120 motion-reduce:animate-none"
      >
        <MenuPrimitive.RadioGroup value={choice.model} onValueChange={(id) => onChoice(chooseModel(models, choice, id))}>
          {models.map((option) => (
            <MenuPrimitive.RadioItem key={option.id} value={option.id} className={ROW}>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate">{shortModelLabel(option)}</span>
                {modelBlurb(option) && <span className="truncate text-xs text-muted-foreground">{modelBlurb(option)}</span>}
              </span>
              <MenuPrimitive.ItemIndicator className="shrink-0">
                <Check className="size-4" aria-hidden="true" />
              </MenuPrimitive.ItemIndicator>
            </MenuPrimitive.RadioItem>
          ))}
        </MenuPrimitive.RadioGroup>

        <DropdownMenuSeparator className="my-1.5" />

        <MenuPrimitive.CheckboxItem
          checked={choice.thinking}
          disabled={locked}
          onCheckedChange={(on) => onChoice(chooseThinking(models, choice, on === true))}
          // A switch: flipping it keeps the menu open, as claude.ai's does.
          onSelect={(event) => event.preventDefault()}
          className={ROW}
        >
          <span className="flex min-w-0 flex-1 flex-col">
            <span>Extended thinking</span>
            <span className="text-xs text-muted-foreground">{locked ? alwaysThinksWords(model) : 'Think longer for complex tasks'}</span>
          </span>
          <Switch on={choice.thinking} />
        </MenuPrimitive.CheckboxItem>

        <MenuPrimitive.Item
          aria-expanded={effortOpen}
          aria-controls={effortOpen ? effortsId : undefined}
          onSelect={(event) => {
            event.preventDefault();
            setEffortOpen((now) => !now);
          }}
          onKeyDown={(event) => {
            if (event.key === 'ArrowRight' && !effortOpen) setEffortOpen(true);
            if (event.key === 'ArrowLeft' && effortOpen) setEffortOpen(false);
          }}
          className={ROW}
        >
          <span className="flex-1">Effort</span>
          <span className="text-xs text-muted-foreground">{EFFORT_WORDS[choice.effort]}</span>
          <ChevronDown className={cn('size-4 text-muted-foreground transition-transform duration-150', effortOpen && 'rotate-180')} aria-hidden="true" />
        </MenuPrimitive.Item>
        {effortOpen && (
          <MenuPrimitive.RadioGroup
            id={effortsId}
            aria-label="Effort"
            value={choice.effort}
            onValueChange={(effort) => onChoice(chooseEffort(models, choice, effort as ChatEffort))}
            className="ml-2 border-l border-border pl-1.5 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-top-1 motion-safe:duration-120"
          >
            {EFFORTS.filter((effort) => model.efforts.includes(effort)).map((effort) => {
              const why = effortBlockedReason(model, effort, choice.thinking);
              return (
                <MenuPrimitive.RadioItem key={effort} value={effort} disabled={why !== null} className={cn(ROW, 'py-1.5')}>
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span>{EFFORT_WORDS[effort]}</span>
                    {why && <span className="text-xs text-muted-foreground">{why}</span>}
                  </span>
                  <MenuPrimitive.ItemIndicator className="shrink-0">
                    <Check className="size-4" aria-hidden="true" />
                  </MenuPrimitive.ItemIndicator>
                </MenuPrimitive.RadioItem>
              );
            })}
          </MenuPrimitive.RadioGroup>
        )}

        {footnote && (
          <>
            <DropdownMenuSeparator className="my-1.5" />
            <p data-slot="chat-model-footnote" className="px-2.5 pt-0.5 pb-1 text-xs leading-snug text-muted-foreground">
              {footnote}
            </p>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The switch drawn inside the Extended thinking row (the row itself is the checkbox people use). */
function Switch({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      data-state={on ? 'on' : 'off'}
      className={cn('relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full transition-colors duration-150', on ? 'bg-primary' : 'bg-input/60')}
    >
      <span className={cn('absolute left-0.5 size-3.5 rounded-full bg-background shadow-sm transition-transform duration-150', on && 'translate-x-3.5')} />
    </span>
  );
}
