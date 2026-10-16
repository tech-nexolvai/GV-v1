import { useId, useRef } from 'react';
import { ArrowUp, Brain, ChevronDown, Gauge, Square } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import type { ChatEffort, ChatModel } from '@/lib/chat-api';
import {
  alwaysThinksWords,
  chooseEffort,
  chooseModel,
  chooseThinking,
  EFFORT_WORDS,
  EFFORTS,
  effortBlockedReason,
  shortModelLabel,
  type ChatChoice,
} from '@/lib/chat-settings';
import { cn } from '@/lib/utils';

/** The most a message may hold (the server refuses more). */
export const MESSAGE_MAX = 8000;
/** The composer grows with its words up to this height, then scrolls. */
const MAX_HEIGHT = 240;

const CONTROL =
  'inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-2 text-[13px] text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none data-[state=open]:bg-accent data-[state=open]:text-foreground';

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
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
}

/**
 * The composer (#1464), like claude.ai's: a box that grows with the words (Enter sends, Shift+Enter
 * starts a new line), and under it the Model and Effort menus and the Thinking switch, then Send, or
 * Stop while a message runs. Opus 5.5 always thinks, so its switch is locked on and says so; with
 * thinking off, the efforts above High cannot be picked.
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
  textareaRef,
}: ChatComposerProps) {
  const fieldId = useId();
  const thinkingWordsId = useId();
  const ownRef = useRef<HTMLTextAreaElement>(null);
  const ref = textareaRef ?? ownRef;
  const model = choice ? models.find((candidate) => candidate.id === choice.model) ?? null : null;
  const canSend = !running && !blocked && value.trim().length > 0;
  const locked = model?.thinking === 'always';

  function grow(box: HTMLTextAreaElement | null) {
    if (!box) return;
    box.style.height = 'auto';
    box.style.height = `${Math.min(box.scrollHeight, MAX_HEIGHT)}px`;
  }

  function send() {
    if (!canSend) return;
    onSend();
    window.requestAnimationFrame(() => grow(ref.current));
  }

  return (
    <form
      data-slot="chat-composer"
      aria-label="Message the assistant"
      className="grid gap-1 rounded-[20px] border bg-background px-2.5 pt-2 pb-2 shadow-xs focus-within:border-ring/60"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <label htmlFor={fieldId} className="sr-only">
        Your message
      </label>
      <textarea
        id={fieldId}
        ref={ref}
        rows={1}
        value={value}
        maxLength={MESSAGE_MAX}
        placeholder={placeholder}
        onChange={(event) => {
          onChange(event.target.value);
          grow(event.target);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            send();
          }
        }}
        className="min-h-[44px] w-full resize-none border-0 bg-transparent px-1.5 py-1.5 text-[15px] leading-[1.5] text-foreground outline-none placeholder:text-muted-foreground"
        style={{ maxHeight: MAX_HEIGHT }}
      />
      <div className="flex min-w-0 items-center gap-0.5">
        {model && choice ? (
          <>
            <DropdownMenu>
              <DropdownMenuTrigger className={CONTROL} aria-label={`Model: ${model.label}`}>
                <span className="max-w-[9rem] truncate">{shortModelLabel(model)}</span>
                <ChevronDown className="size-3.5" aria-hidden="true" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-56 font-sans">
                <DropdownMenuLabel>Model</DropdownMenuLabel>
                <DropdownMenuRadioGroup value={choice.model} onValueChange={(id) => onChoice(chooseModel(models, choice, id))}>
                  {models.map((option) => (
                    <DropdownMenuRadioItem key={option.id} value={option.id}>
                      <span className="flex flex-col">
                        <span>{option.label}</span>
                        <span className="text-xs text-muted-foreground">{option.thinking === 'always' ? 'Always thinks' : 'Thinking optional'}{option.default ? ' · default' : ''}</span>
                      </span>
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            <DropdownMenu>
              <DropdownMenuTrigger className={CONTROL} aria-label={`Effort: ${EFFORT_WORDS[choice.effort]}`}>
                <Gauge className="size-3.5" aria-hidden="true" />
                <span className="hidden min-[380px]:inline">{EFFORT_WORDS[choice.effort]}</span>
                <ChevronDown className="size-3.5" aria-hidden="true" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-60 font-sans">
                <DropdownMenuLabel>Effort</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuRadioGroup value={choice.effort} onValueChange={(effort) => onChoice(chooseEffort(models, choice, effort as ChatEffort))}>
                  {EFFORTS.filter((effort) => model.efforts.includes(effort)).map((effort) => {
                    const why = effortBlockedReason(model, effort, choice.thinking);
                    return (
                      <DropdownMenuRadioItem key={effort} value={effort} disabled={why !== null}>
                        <span className="flex flex-col">
                          <span>{EFFORT_WORDS[effort]}</span>
                          {why && <span className="text-xs text-muted-foreground">{why}</span>}
                        </span>
                      </DropdownMenuRadioItem>
                    );
                  })}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-pressed={choice.thinking}
                  // Locked on, but still focusable, so its reason can be read.
                  aria-disabled={locked || undefined}
                  aria-describedby={locked ? thinkingWordsId : undefined}
                  onClick={() => {
                    if (!locked) onChoice(chooseThinking(models, choice, !choice.thinking));
                  }}
                  className={cn(CONTROL, choice.thinking && 'bg-accent/70 text-foreground', locked && 'cursor-default hover:bg-accent/70')}
                >
                  <Brain className="size-3.5" aria-hidden="true" />
                  <span className="hidden min-[380px]:inline">Thinking</span>
                  <span className="sr-only">{choice.thinking ? ' on' : ' off'}</span>
                </button>
              </TooltipTrigger>
              <TooltipContent side="top">
                {locked ? alwaysThinksWords(model) : choice.thinking ? `Turn thinking off (effort up to ${EFFORT_WORDS[model.thinking_off_max_effort ?? 'max']})` : 'Turn thinking on'}
              </TooltipContent>
            </Tooltip>
            {locked && <span id={thinkingWordsId} className="sr-only">{alwaysThinksWords(model)}</span>}
          </>
        ) : null}

        <span className="flex-1" />
        {value.length > MESSAGE_MAX - 500 && (
          <span className="mr-1 text-xs text-muted-foreground"><span className="num">{value.length}</span>/<span className="num">{MESSAGE_MAX}</span></span>
        )}
        {running ? (
          <button
            type="button"
            onClick={onStop}
            disabled={stopping}
            aria-label={stopping ? 'Stopping' : 'Stop'}
            title={stopping ? 'Stopping' : 'Stop'}
            className="grid size-[34px] shrink-0 place-items-center rounded-full bg-primary text-primary-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:opacity-50"
          >
            <Square className="size-3 fill-current" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="submit"
            aria-label="Send"
            title={blocked ?? 'Send (Enter)'}
            disabled={!canSend}
            className="grid size-[34px] shrink-0 place-items-center rounded-full bg-primary text-primary-foreground transition-opacity focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-default disabled:opacity-25"
          >
            <ArrowUp className="size-4" aria-hidden="true" />
          </button>
        )}
      </div>
    </form>
  );
}
