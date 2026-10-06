import { useEffect, useRef } from 'react';
import type { DownloadState } from './reportDownload';

type Status = 'loading' | 'error' | 'not_requested' | 'preparing' | 'ready' | 'failed';

export function SignedDownloadsView({ status, busy, request, refresh, download, receipt = { status: 'idle' } }: {
  status: Status;
  busy: boolean;
  request: () => void;
  refresh: () => void;
  download: (format: 'pdf' | 'workbook' | 'redline') => void;
  receipt?: DownloadState;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    function outside(event: PointerEvent) {
      if (menu.current && event.target instanceof Node && !menu.current.contains(event.target)) menu.current.open = false;
    }
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, []);
  return <section className="signed-downloads" aria-label="Signed exports">
    {status === 'ready' ? <details className="signed-downloads__menu" ref={menu} onKeyDown={(event) => {
      if (event.key === 'Escape' && menu.current?.open) {
        menu.current.open = false;
        menu.current.querySelector('summary')?.focus();
      }
    }}>
      <summary>Signed reports</summary>
      <div className="signed-downloads__options">
        <p>Signed review ready. Each report includes the review record and sign-off.</p>
        <button type="button" disabled={receipt.status === 'loading'} onClick={() => download('pdf')}>Download PDF <small>Findings and recorded reasons</small></button>
        <button type="button" disabled={receipt.status === 'loading'} onClick={() => download('workbook')}>Download workbook <small>Findings, values and review sheets</small></button>
        <button type="button" disabled={receipt.status === 'loading'} onClick={() => download('redline')}>Download redline <small>Marked drawing and review record</small></button>
      </div>
    </details> : <>
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
    {receipt.status === 'loading' && <p role="status">Requesting {receipt.format} report…</p>}
    {receipt.status === 'started' && <p role="status">Download started. Check your browser downloads.</p>}
    {receipt.status === 'error' && <p role="alert">The report could not be downloaded: {receipt.message}</p>}
  </section>;
}
