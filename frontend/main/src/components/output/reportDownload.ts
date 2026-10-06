export type ReportFormat = 'pdf' | 'workbook' | 'redline';
export type DownloadState =
  | { status: 'idle' }
  | { status: 'loading'; format: ReportFormat }
  | { status: 'started'; format: ReportFormat }
  | { status: 'error'; format: ReportFormat; message: string };

/** Deliver the server's exact artifact, never a reconstructed report or an empty response. */
export async function receiveReport(
  format: ReportFormat,
  load: (format: ReportFormat) => Promise<Blob>,
  deliver: (blob: Blob) => void,
): Promise<void> {
  const blob = await load(format);
  if (blob.size === 0) throw new Error('The server returned an empty report. Please retry.');
  deliver(blob);
}
