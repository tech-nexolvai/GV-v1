import { useId, type ReactNode } from 'react';

/** A shared reading width and heading for supporting reviewer pages. */
export function PageFrame({ title, description, actions, className = '', children }: {
  title: string;
  description: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  const titleId = useId();
  return (
    <section className={`page-frame ${className}`} aria-labelledby={titleId}>
      <div className="page-frame__inner">
        <header className="page-frame__header">
          <div className="page-frame__heading">
            <h1 id={titleId}>{title}</h1>
            <p className="page-frame__description">{description}</p>
          </div>
          {actions && <div className="page-frame__actions">{actions}</div>}
        </header>
        <div className="page-frame__body">{children}</div>
      </div>
    </section>
  );
}

/** Failure remains distinct from an empty result; retry repeats the same request. */
export function PageLoadError({ title, message, onRetry }: {
  title: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="page-frame__error" role="alert">
      <h2>{title}</h2>
      <p>{message}</p>
      <button type="button" className="btn btn--ghost" onClick={onRetry}>Try again</button>
    </div>
  );
}
