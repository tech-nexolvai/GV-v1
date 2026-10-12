/**
 * An earlier revision, read-only (#1180): a banner saying so, and its countertop results exactly as
 * they were recorded (`GET …/countertop-results?revision=N`). Nothing here records anything; the
 * API refuses a write to a replaced revision as well.
 */

import { useState } from 'react';
import { Download, Lock } from 'lucide-react';
import {
  downloadPdfReport,
  downloadRedline,
  downloadReport,
  getCountertopResults,
  type Revision,
} from '@/api/client';
import { useAsync } from '@/api/useAsync';
import { Button } from '@/components/ui/button';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

export function ReadOnlyBanner({ revision, newest, onBack }: { revision: Revision; newest: number; onBack: () => void }) {
  return (
    <div
      role="status"
      data-slot-name="read-only-banner"
      className="flex flex-wrap items-center gap-3 border-b bg-muted px-4 py-3 font-sans text-sm sm:px-6"
    >
      <Lock className="size-4 shrink-0" aria-hidden="true" />
      <p className="min-w-0 flex-1">
        <span className="font-medium">
          Revision <span className="num">{revision.revision_number}</span> is read-only.
        </span>{' '}
        It was replaced by revision <span className="num">{revision.superseded_by_revision_number ?? newest}</span>
        {revision.signed_off_by ? <>; it was signed off by {revision.signed_off_by}</> : null}. Nothing more can be
        recorded on it.
      </p>
      <Button size="sm" variant="outline" onClick={onBack}>
        Back to revision <span className="num">{newest}</span>
      </Button>
    </div>
  );
}

type SignedFile = 'pdf' | 'workbook' | 'redline';

const SIGNED_FILES: readonly { id: SignedFile; label: string }[] = [
  { id: 'pdf', label: 'Signed PDF' },
  { id: 'workbook', label: 'Workbook' },
  { id: 'redline', label: 'Redline' },
];

/** An earlier revision's signed files, exactly as they were signed (#1180). */
export function SignedFiles({ projectId, packageId, revision }: { projectId: string; packageId: string; revision: Revision }) {
  const [state, setState] = useState<{ busy: SignedFile | null; message: string | null }>({ busy: null, message: null });
  if (!revision.signed_off_at) return null;
  const number = revision.revision_number;
  async function fetchFile(file: SignedFile) {
    setState({ busy: file, message: null });
    try {
      const blob =
        file === 'pdf'
          ? await downloadPdfReport(projectId, packageId, number)
          : file === 'workbook'
            ? await downloadReport(projectId, packageId, number)
            : await downloadRedline(projectId, packageId, number);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `gv-review-${packageId}-r${number}${file === 'redline' ? '-redline' : ''}.${file === 'workbook' ? 'xlsx' : 'pdf'}`;
      link.click();
      URL.revokeObjectURL(url);
      setState({ busy: null, message: 'Download started. Check your browser downloads.' });
    } catch (failure) {
      setState({ busy: null, message: `The file could not be downloaded: ${failure instanceof Error ? failure.message : String(failure)}` });
    }
  }
  return (
    <section aria-label={`Signed files of revision ${number}`} className="mb-4 flex flex-col gap-2">
      <h2 className="text-base font-medium">
        Signed files of revision <span className="num">{number}</span>
      </h2>
      <div className="flex flex-wrap gap-2">
        {SIGNED_FILES.map((file) => (
          <Button key={file.id} size="sm" variant="outline" disabled={state.busy !== null} onClick={() => void fetchFile(file.id)}>
            <Download aria-hidden="true" /> {file.label}
          </Button>
        ))}
      </div>
      {state.message && <p className="text-sm text-muted-foreground" role="status">{state.message}</p>}
    </section>
  );
}

export function EarlierRevisionView({
  projectId,
  packageId,
  revision,
  newest,
  onBack,
}: {
  projectId: string;
  packageId: string;
  revision: Revision;
  newest: number;
  onBack: () => void;
}) {
  const results = useAsync(
    () => getCountertopResults(projectId, packageId, revision.revision_number),
    [projectId, packageId, revision.revision_number],
  );
  return (
    <div data-tw className="flex min-h-0 flex-1 flex-col font-sans">
      <ReadOnlyBanner revision={revision} newest={newest} onBack={onBack} />
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <SignedFiles projectId={projectId} packageId={packageId} revision={revision} />
        <h2 className="mb-3 text-base font-medium">
          Countertop results of revision <span className="num">{revision.revision_number}</span>
        </h2>
        {results.status === 'loading' && <Skeleton className="h-24 w-full" aria-label="Loading results" />}
        {results.status === 'error' && (
          <p className="text-sm" role="alert">The results could not be loaded: {results.error.message}</p>
        )}
        {results.status === 'ready' && results.data.items.length === 0 && (
          <p className="text-sm text-muted-foreground">No countertop was recorded on this revision.</p>
        )}
        {results.status === 'ready' && results.data.items.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Page</TableHead>
                <TableHead>Countertop</TableHead>
                <TableHead>Result</TableHead>
                <TableHead className="hidden sm:table-cell">Printed</TableHead>
                <TableHead className="hidden sm:table-cell">Needed</TableHead>
                <TableHead className="hidden md:table-cell">Decided by</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {results.data.items.map((item) => (
                <TableRow key={item.row_id}>
                  <TableCell className="num">{item.page_number}</TableCell>
                  <TableCell className="whitespace-normal">{item.label}</TableCell>
                  <TableCell>
                    {item.outcome ? <OutcomeBadge outcome={item.outcome} /> : <span className="text-muted-foreground">Not checked</span>}
                  </TableCell>
                  <TableCell className="num hidden sm:table-cell">{item.printed_overall?.display ?? '—'}</TableCell>
                  <TableCell className="num hidden sm:table-cell">{item.expected_total?.display ?? '—'}</TableCell>
                  <TableCell className="hidden md:table-cell">
                    {item.reviewer_decision ? `${item.reviewer_decision.actor}` : '—'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
