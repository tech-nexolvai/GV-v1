/**
 * Plain words with every number set in the number face.
 *
 * Moved here unchanged on 2026-10-28 from `components/simple-review/words.tsx` (the old Review list),
 * because the chat uses it (`chat-trust`, `chat-preflight`).
 */
import { splitNumbers } from '@/lib/numbers';

/** Plain words with every number in it set in the number face (tabular Plex Mono). */
export function NumText({ text, className, 'data-slot': slot }: { text: string; className?: string; 'data-slot'?: string }) {
  return (
    <span data-slot={slot} className={className}>
      {splitNumbers(text).map((part, index) =>
        part.number ? (
          <span key={index} className="num">
            {part.text}
          </span>
        ) : (
          part.text
        ),
      )}
    </span>
  );
}
