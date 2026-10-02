/** Tab-local navigation metadata only. Never store document records or reading values here. */
export type PositionStorage = Pick<Storage, 'getItem' | 'setItem'>;
export interface DocumentPosition { cursors: readonly string[]; notice: string | null }
const unavailable = 'This browser cannot remember the document page after a reload. Navigation still works during this visit.';
const key = (project: string) => `gv:documents-position:${project}`;

export function readDocumentPosition(storage: PositionStorage | null, project: string): DocumentPosition {
  if (!storage) return { cursors: [], notice: unavailable };
  try {
    const raw = storage.getItem(key(project));
    if (raw === null) return { cursors: [], notice: null };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null || !('version' in parsed) || parsed.version !== 1 ||
      !('cursors' in parsed) || !Array.isArray(parsed.cursors) ||
      !parsed.cursors.every((cursor: unknown) => typeof cursor === 'string' && cursor.length > 0) ||
      new Set(parsed.cursors).size !== parsed.cursors.length) {
      return { cursors: [], notice: 'The saved document position was invalid. Starting from the first page.' };
    }
    return { cursors: parsed.cursors, notice: null };
  } catch {
    return { cursors: [], notice: 'The saved document position could not be read. Starting from the first page.' };
  }
}

export function saveDocumentPosition(storage: PositionStorage | null, project: string, cursors: readonly string[]): string | null {
  try {
    if (!storage) return unavailable;
    storage.setItem(key(project), JSON.stringify({ version: 1, cursors }));
    return null;
  } catch { return unavailable; }
}
