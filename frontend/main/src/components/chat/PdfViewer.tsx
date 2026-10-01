import { useEffect, useRef, useState } from 'react';
import * as pdfjs from 'pdfjs-dist';
import { ZoomIn, ZoomOut, Maximize } from 'lucide-react';
import './PdfViewer.css';

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.mjs',
  import.meta.url,
).toString();

interface PdfViewerProps {
  url: string;
  polygon?: [string, string][];
  pageIndex?: number;
}

export function PdfViewer({ url, polygon, pageIndex = 1 }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1.5);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let renderTask: pdfjs.RenderTask | null = null;
    
    const loadPdf = async () => {
      try {
        setLoading(true);
        setError(null);
        
        const loadingTask = pdfjs.getDocument({ url });
        const pdf = await loadingTask.promise;
        
        if (!active) return;
        
        const pageNumber = Math.max(1, Math.min(pageIndex, pdf.numPages));
        const page = await pdf.getPage(pageNumber);
        
        if (!active) return;
        
        const viewport = page.getViewport({ scale });
        const canvas = canvasRef.current;
        if (!canvas) return;
        
        const context = canvas.getContext('2d');
        if (!context) return;
        
        canvas.height = viewport.height;
        canvas.width = viewport.width;
        
        const renderContext = {
          canvasContext: context,
          canvas: canvas,
          viewport: viewport,
        };
        
        renderTask = page.render(renderContext);
        await renderTask.promise;
        
        if (active) {
          setLoading(false);
          
          if (polygon && polygon.length > 0) {
            context.beginPath();
            context.strokeStyle = '#e11d48'; // red-600
            context.lineWidth = 3 / scale;
            context.fillStyle = 'rgba(225, 29, 72, 0.2)';
            
            const [first, ...rest] = polygon;
            // pdfjs viewport handles transformations, but if polygon is raw PDF points we need to apply viewport transform
            // Adjust according to how GV backend delivers points. Let's assume standard PDF coordinates
            const pt = viewport.convertToViewportPoint(parseFloat(first[0]), parseFloat(first[1]));
            context.moveTo(pt[0], pt[1]);
            
            for (const p of rest) {
              const pt2 = viewport.convertToViewportPoint(parseFloat(p[0]), parseFloat(p[1]));
              context.lineTo(pt2[0], pt2[1]);
            }
            context.closePath();
            context.fill();
            context.stroke();
            
            if (containerRef.current) {
              const bounds = polygon.reduce((acc, p) => {
                const pt = viewport.convertToViewportPoint(parseFloat(p[0]), parseFloat(p[1]));
                return {
                  minX: Math.min(acc.minX, pt[0]),
                  maxX: Math.max(acc.maxX, pt[0]),
                  minY: Math.min(acc.minY, pt[1]),
                  maxY: Math.max(acc.maxY, pt[1])
                };
              }, { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity });
              
              const scrollY = Math.max(0, bounds.minY - containerRef.current.clientHeight / 2);
              const scrollX = Math.max(0, bounds.minX - containerRef.current.clientWidth / 2);
              containerRef.current.scrollTo({ top: scrollY, left: scrollX, behavior: 'smooth' });
            }
          }
        }
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : String(err));
          setLoading(false);
        }
      }
    };
    
    loadPdf();
    
    return () => {
      active = false;
      if (renderTask) {
        renderTask.cancel();
      }
    };
  }, [url, scale, pageIndex, polygon]);

  return (
    <div className="pdf-viewer">
      <div className="pdf-viewer__controls">
        <button className="btn btn--icon btn--ghost" onClick={() => setScale(s => Math.max(0.5, s - 0.25))} title="Zoom Out">
          <ZoomOut size={16} />
        </button>
        <span className="pdf-viewer__scale">{Math.round(scale * 100)}%</span>
        <button className="btn btn--icon btn--ghost" onClick={() => setScale(s => Math.min(4, s + 0.25))} title="Zoom In">
          <ZoomIn size={16} />
        </button>
        <button className="btn btn--icon btn--ghost" onClick={() => setScale(1.5)} title="Reset Zoom">
          <Maximize size={16} />
        </button>
      </div>
      <div className="pdf-viewer__container" ref={containerRef}>
        {loading && <div className="pdf-viewer__overlay">Rendering PDF...</div>}
        {error && <div className="pdf-viewer__error">{error}</div>}
        <canvas ref={canvasRef} className="pdf-viewer__canvas" />
      </div>
    </div>
  );
}
