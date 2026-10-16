/**
 * Chat v2/v3 (#1483): the answer's exports, made by code, never by the AI: the marked-up PDF (made by
 * the server from the findings) and the findings as a CSV Excel opens (made here from the findings).
 */

import { ApiError } from '@/api/client';
import type { ChatFinding } from '@/lib/chat-api';
import { findingsCsv } from '@/lib/chat-trust';

/** Start a browser download of a file the server made. */
export function downloadFile(href: string, name: string) {
  const link = document.createElement('a');
  link.href = href;
  link.download = name;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/** Why the marked-up PDF could not be made, in plain words (the server's own words when it sent some). */
export function markupFailureWords(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return 'This answer was not found on the server. Reload the chat and try again.';
    if (error.status >= 500 || error.code === 'unreadable_response' || !error.message) return 'The marked-up PDF could not be made right now. Try again.';
    return error.message;
  }
  if (error instanceof TypeError) return 'The server could not be reached. Check the connection and try again.';
  return 'The marked-up PDF could not be made. Try again.';
}

/** Save the findings, trust columns included, as a CSV Excel opens. Made here by code from the findings. */
export function downloadFindingsCsv(findings: readonly ChatFinding[], name: string) {
  const url = URL.createObjectURL(new Blob([findingsCsv(findings)], { type: 'text/csv;charset=utf-8' }));
  downloadFile(url, name);
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
