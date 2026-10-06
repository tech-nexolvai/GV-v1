type Status = 'loading' | 'error' | 'not_requested' | 'preparing' | 'ready' | 'failed';

export function SignedDownloadsView({ status, busy, request, refresh, download }: {
  status: Status;
  busy: boolean;
  request: () => void;
  refresh: () => void;
  download: (format: 'pdf' | 'workbook' | 'redline') => void;
}) {
  return <section aria-label="Signed exports">
    {status === 'ready' ? <>
      <span>Signed review ready</span>
      <button type="button" className="btn btn--ghost" onClick={() => download('pdf')}>Download PDF</button>
      <button type="button" className="btn btn--ghost" onClick={() => download('workbook')}>Download workbook</button>
      <button type="button" className="btn btn--ghost" onClick={() => download('redline')}>Download redline</button>
    </> : <>
      <p role={status === 'error' || status === 'failed' ? 'alert' : 'status'}>{status === 'failed'
        ? 'Signed export generation failed. No final files were published. Ask the operator to check the worker, then check availability again.'
        : status === 'error'
        ? 'Could not check signed exports. No before-review file has been downloaded.'
        : status === 'not_requested'
          ? 'This approval needs signed exports. The before-review files are not final reports.'
          : 'Signed exports are not ready. Check availability again after the worker prepares them.'}</p>
      {status === 'not_requested' && <button type="button" className="btn btn--ghost" disabled={busy} onClick={request}>{busy ? 'Requesting…' : 'Prepare signed exports'}</button>}
      <button type="button" className="btn btn--ghost" onClick={refresh}>Check availability</button>
    </>}
  </section>;
}
