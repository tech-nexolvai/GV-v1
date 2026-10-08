import { useEffect, useRef, useState } from 'react';
import { downloadPagePicture } from '../../api/client';
import type { Finding } from '../../data/types';
import { drawingPoints } from './reviewerResults';
import './DrawingResultPanel.css';

/** Exact immutable document version; never substitute a same-numbered page from another file. */
/** What the panel needs to place a result: a name, and a row outline or evidence location. */
export type DrawingTarget = Pick<Finding, 'name' | 'scope_label' | 'row_location' | 'shop_evidence' | 'arch_evidence'>;

export function DrawingResultPanel({ finding, projectId, packageId, onClose }: { finding: DrawingTarget; projectId: string; packageId: string; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const viewport = useRef<HTMLDivElement>(null);
  const picture = useRef<HTMLImageElement>(null);
  const evidence = finding.shop_evidence ?? finding.arch_evidence;
  const version = finding.row_location?.document_version_id ?? evidence?.document_version_id;
  const page = finding.row_location?.page_number ?? evidence?.page;
  const polygon = finding.row_location?.polygon ?? evidence?.polygon;
  useEffect(() => {
    let live = true;
    let created: string | null = null;
    if (version && page) downloadPagePicture(projectId, packageId, page, version).then(blob => {
      if (live) { created = URL.createObjectURL(blob); setUrl(created); }
    }).catch((caught: unknown) => { if (live) setError(caught instanceof Error ? caught.message : String(caught)); });
    return () => { live = false; if (created) URL.revokeObjectURL(created); };
  }, [projectId, packageId, version, page]);
  let points: [number, number][] = [];
  try { if (polygon) points = drawingPoints(polygon, 1, 1); } catch { /* Invalid geometry is disclosed, never guessed. */ }
  function focusOutline() {
    if (!viewport.current || !picture.current || !points.length) return;
    const x = Math.min(...points.map(p => p[0]));
    const y = Math.min(...points.map(p => p[1]));
    viewport.current.scrollTo({ left: Math.max(0, x * picture.current.width - viewport.current.clientWidth / 4), top: Math.max(0, y * picture.current.height - viewport.current.clientHeight / 3) });
  }
  return <section className="result-drawing" aria-label="Result on drawing">
    <header><div><strong>{finding.scope_label ?? finding.name}</strong><p>Page {page ?? 'not located'} · recorded row outline</p></div><button className="btn btn--subtle" onClick={onClose}>Close drawing</button></header>
    <p className="result-drawing__hint">Vendor-layer page picture. Markup baked into that layer may remain. The dashed outline only locates this result.</p>
    {error ? <p role="alert">Drawing could not be loaded: {error}</p> : url && page ? <>
      <div className="result-drawing__controls"><button className="btn btn--subtle" onClick={() => setZoom(z => Math.max(0.5, z - 0.25))} aria-label="Zoom out">−</button><span>{Math.round(zoom * 100)}%</span><button className="btn btn--subtle" onClick={() => setZoom(z => Math.min(3, z + 0.25))} aria-label="Zoom in">+</button><button className="btn btn--ghost" onClick={focusOutline}>Find outline</button></div>
      {!points.length && <p role="alert">The stored outline is unavailable. No substitute location is drawn.</p>}
      <div className="result-drawing__viewport" ref={viewport}>
        <div className="result-drawing__page" style={{ width: `${100 * zoom}%`, minWidth: `${800 * zoom}px` }}>
          <img ref={picture} src={url} alt={`Vendor drawing page ${page}`} onLoad={focusOutline} />
          {points.length > 0 && <svg viewBox="0 0 1 1" preserveAspectRatio="none" role="img" aria-label={`Recorded row outline on page ${page}`}><polygon points={points.map(p => p.join(',')).join(' ')} /></svg>}
        </div>
      </div>
    </> : <p role="status">{version ? 'Loading the recorded drawing…' : 'No stored drawing location.'}</p>}
  </section>;
}
