import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

/** Secondary context only; never owns review state or actions. */
export function ReviewPackageDetails({ packageId, projectId, children }: {
  packageId: string;
  projectId: string;
  children: ReactNode;
}) {
  const disclosure = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    function close(event: PointerEvent | KeyboardEvent) {
      const element = disclosure.current;
      if (!element?.open) return;
      if (event instanceof KeyboardEvent) {
        if (event.key !== 'Escape') return;
        element.open = false;
        element.querySelector('summary')?.focus();
      } else if (event.target instanceof Node && !element.contains(event.target)) {
        element.open = false;
      }
    }
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    };
  }, []);

  return <details className="review-package-details" ref={disclosure}
    onBlur={(event) => {
      if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) {
        event.currentTarget.open = false;
      }
    }}>
    <summary>Details &amp; steps</summary>
    <div className="review-package-details__body" tabIndex={0}
      role="region" aria-label="Package details and review steps">
      <p className="review-package-details__title">Review steps</p>
      {children}
      <p className="review-package-details__title">How this review works</p>
      <p>Recorded values → deterministic checks → optional AI narration.</p>
      <p className="review-package-details__title">Record IDs</p>
      <dl>
        <dt>Package</dt><dd>{packageId}</dd>
        <dt>Project</dt><dd>{projectId}</dd>
      </dl>
    </div>
  </details>;
}
