/** What the drawing pane shows for one page: loading, an error, or the picture. No fetching here. */
export function PageDrawingView({
  pageNumber,
  url,
  error,
}: {
  pageNumber: number;
  url?: string;
  error?: string;
}) {
  return (
    <aside className="page-drawing" aria-label={`Drawing, page ${pageNumber}`}>
      <p className="page-drawing__title">Drawing · page {pageNumber}</p>
      {url === undefined && error === undefined && (
        <p className="page-drawing__status">Loading the drawing…</p>
      )}
      {error !== undefined && (
        <p className="page-drawing__status" role="alert">
          The drawing could not be shown. {error}
        </p>
      )}
      {url !== undefined && (
        <a href={url} target="_blank" rel="noreferrer" title="Open the page full size">
          <img className="page-drawing__image" src={url} alt={`Drawing page ${pageNumber}`} />
        </a>
      )}
    </aside>
  );
}
