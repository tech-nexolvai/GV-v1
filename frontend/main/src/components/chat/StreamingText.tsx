/**
 * Reveals an assistant reply the way it was written rather than pasting it in one frame.
 *
 * **This is presentation, not fabrication.** The text is exactly the string the API returned; this
 * controls how fast it appears and nothing else. The endpoint answers in a single response — there
 * is no token stream on the wire — so a reply used to land as a finished wall of text, which read as
 * a canned lookup rather than as something composed for the question asked.
 *
 * **Why it reveals whole tokens and not characters.** The content carries `**bold**` markers that
 * `renderMarkdown` turns into elements. A character-by-character reveal puts a bare `**` on screen
 * for a few frames every time a bold span begins, and a half-open marker renders as literal
 * asterisks. So the tokenizer keeps each `**…**` span atomic and reveals two or three tokens a
 * frame, which also lands closer to how a model actually emits text — in words, not letters.
 *
 * Clicking the text completes it immediately. Under `prefers-reduced-motion` it is complete on the
 * first frame: an animation somebody has asked not to see should not also make them wait.
 */

import { useEffect, useMemo, useState } from 'react';
import './StreamingText.css';

/** Tokens revealed per tick. Three at ~24ms reads as quick and deliberate; one is a teleprinter. */
const TOKENS_PER_TICK = 3;
const TICK_MS = 24;

interface StreamingTextProps {
  text: string;
  /**
   * `false` renders the whole string at once — for every message already in the thread. Only the
   * reply that just arrived streams; replaying the history on each render would be theatre.
   */
  stream: boolean;
  /**
   * Renders the revealed prefix. Passed in rather than fixed here so this component knows nothing
   * about markdown — it decides *when* text is visible, and the caller decides what it looks like.
   */
  render: (visible: string) => React.ReactNode;
}

export function StreamingText({ text, stream, render }: StreamingTextProps) {
  const tokens = useMemo(() => tokenize(text), [text]);
  const reduced = usePrefersReducedMotion();
  const skip = !stream || reduced;

  const [shown, setShown] = useState(() => (skip ? tokens.length : 0));

  /**
   * Restart the reveal when the text changes, during render rather than in an effect.
   *
   * This is React's documented way to reset state from a prop: comparing against the previous value
   * and calling the setter while rendering. The effect version — `setShown(0)` inside
   * `useEffect([tokens])` — paints the finished previous message for one frame before resetting,
   * which shows the wrong answer under the new question every time a second one is asked.
   */
  const [renderedTokens, setRenderedTokens] = useState(tokens);
  if (renderedTokens !== tokens) {
    setRenderedTokens(tokens);
    setShown(skip ? tokens.length : 0);
  }

  // `skip ||`, not just the token count. If the reader turns reduced-motion on partway through a
  // reveal, `skip` flips but `tokens` has not changed — so no render-reset fires, the effect below
  // returns early, and `shown` is left wherever it stopped. The message froze half-written, and it
  // froze for precisely the person who had asked for less motion.
  const complete = skip || shown >= tokens.length;

  useEffect(() => {
    if (skip || complete) return;

    const timer = window.setInterval(() => {
      setShown((current) => Math.min(current + TOKENS_PER_TICK, tokens.length));
    }, TICK_MS);

    return () => window.clearInterval(timer);
    // `complete` is deliberately not a dependency: it becomes true through this very timer, and
    // depending on it would tear the interval down and build it again on every tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tokens.length, skip]);

  const visible = complete ? text : tokens.slice(0, shown).join('');

  return (
    <span
      className={`streaming ${complete ? '' : 'streaming--live'}`}
      // Completing on click is the affordance everyone already expects from revealed text. It is
      // not announced, because a reader who does not want it never discovers it is there.
      onClick={() => !complete && setShown(tokens.length)}
    >
      {render(visible)}
      {!complete && <span className="streaming__caret" aria-hidden="true" />}
    </span>
  );
}

/**
 * Splits text so that a `**bold**` span is one indivisible token.
 *
 * Whitespace is kept as its own token rather than trimmed, so joining a prefix reproduces the
 * original spacing and line breaks exactly — the reveal must not reflow the paragraph as it goes.
 * A lone `*` matches last, so an asterisk that is not part of a pair is still shown rather than
 * swallowed.
 */
function tokenize(text: string): string[] {
  const pattern = /\*\*[^*]+\*\*|\s+|[^\s*]+|\*/g;
  return text.match(pattern) ?? [];
}

/**
 * Whether the reader has asked for reduced motion, kept current if they change it mid-session.
 *
 * Read from `matchMedia` rather than assumed once at module load: the setting is a system
 * preference and people do turn it on partway through, usually because something on screen is
 * already bothering them.
 */
function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  );

  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (!query) return;
    const listener = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener('change', listener);
    return () => query.removeEventListener('change', listener);
  }, []);

  return reduced;
}
