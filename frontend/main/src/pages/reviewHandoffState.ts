export type ReportFormat = 'pdf' | 'workbook' | 'redline';
export type DownloadState =
  | { status: 'idle' }
  | { status: 'loading'; format: ReportFormat }
  | { status: 'started'; format: ReportFormat }
  | { status: 'error'; format: ReportFormat; message: string };

export const REPORT_LABELS: Record<ReportFormat, string> = {
  pdf: 'PDF report', workbook: 'Findings workbook', redline: 'Drawing redline',
};

/** Presentation only: the approval endpoint remains the final authority. */
export function handoffAvailability(input: {
  findingsCount: number; needsAction: number; approved: boolean;
  sessionCompleted: boolean; signing: boolean; working: boolean;
}) {
  const signOffDisabled = input.signing || input.findingsCount === 0 || input.needsAction > 0 ||
    input.approved || input.sessionCompleted || input.working;
  const message = input.signing ? 'Recording your sign-off…'
    : input.working ? 'Wait for the current checks to finish.'
      : input.approved ? 'Signed off. Choose a report to download.'
        : input.findingsCount === 0 ? 'Run checks before signing off.'
          : input.needsAction > 0 ? `${input.needsAction} finding${input.needsAction === 1 ? '' : 's'} still need${input.needsAction === 1 ? 's' : ''} your review.`
            : input.sessionCompleted ? 'This review session is closed. Refresh to see its recorded status.'
              : 'Review complete. Sign off to unlock reports.';
  return { signOffDisabled, downloadsDisabled: !input.approved || input.working, message };
}

/** Receipt is not a saved-file claim: only hand off a nonempty server artifact to the browser. */
export async function receiveReport(
  format: ReportFormat,
  load: (format: ReportFormat) => Promise<Blob>,
  deliver: (blob: Blob) => void,
): Promise<void> {
  const blob = await load(format);
  if (blob.size === 0) throw new Error('The server returned an empty report. Please retry.');
  deliver(blob);
}
