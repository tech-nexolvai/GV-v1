import { useCallback, useState } from 'react';
import { projectId } from '../api/config';
import { readDocumentPosition, saveDocumentPosition } from '../pages/documentPosition';
import type { DocumentPosition, PositionStorage } from '../pages/documentPosition';

function storage(): PositionStorage | null {
  try { return window.sessionStorage; } catch { return null; }
}

/** App-level memory survives leaving Documents; session storage survives this tab's refresh. */
export function useDocumentPosition() {
  const [position, setPosition] = useState<DocumentPosition>(() => {
    try { return readDocumentPosition(storage(), projectId()); }
    catch { return { cursors: [], notice: null }; } // Existing page errors explain missing project config.
  });
  const remember = useCallback((cursors: readonly string[]) => {
    let notice: string | null = null;
    try { notice = saveDocumentPosition(storage(), projectId(), cursors); }
    catch { /* Do not hide the page's existing configuration error. */ }
    setPosition({ cursors: [...cursors], notice });
  }, []);
  return [position, remember] as const;
}
