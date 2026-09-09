import { useRef, useState } from 'react';
import { AppShell } from './components/shell/AppShell';
import { ReviewPage } from './pages/ReviewPage';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { ConfirmReadingsPage } from './pages/ConfirmReadingsPage';
import { EnterValuesPage } from './pages/EnterValuesPage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { createPackage } from './api/upload';
import type { UploadProgress } from './api/upload';
import { projectId } from './api/config';
import { X, UploadCloud, Loader2 } from 'lucide-react';
import './design/components.css';

export default function App() {
  const [activePage, setActivePage] = useState<string>('review');
  const [activeSession, setActiveSession] = useState<string>('');
  const [evidencePanel, setEvidencePanel] = useState<React.ReactNode>(null);
  const [pendingMessage, setPendingMessage] = useState<string>('');

  // New package form states
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [vendor, setVendor] = useState('Apex Glass & Stone');
  const [archFile, setArchFile] = useState<File | null>(null);
  const [shopFile, setShopFile] = useState<File | null>(null);
  const archInputRef = useRef<HTMLInputElement>(null);
  const shopInputRef = useRef<HTMLInputElement>(null);
  const [uploadStep, setUploadStep] = useState('');
  const [uploadError, setUploadError] = useState<string | null>(null);

  const [isSimulating, setIsSimulating] = useState(false);

  function handleNavigate(page: string) {
    setActivePage(page);
    setEvidencePanel(null);
  }

  function handleSelectSession(id: string) {
    setActiveSession(id);
    setActivePage('review');
    setEvidencePanel(null);
  }

  function handleOpenReview(packageId: string) {
    // ReviewPage fetches by package id, so that is what the active selection carries. Sessions are
    // listed in the sidebar from the API; there is no local table to look one up in.
    setActiveSession(packageId);
    setActivePage('review');
    setEvidencePanel(null);
  }

  /** Called from WelcomePage — picks a session then queues the message */
  function handleWelcomeStart(sessionId: string) {
    setActiveSession(sessionId);
    setActivePage('review');
    setEvidencePanel(null);
  }

  function handleWelcomeSend(text: string) {
    setPendingMessage(text);
  }

  async function triggerSubmitPipeline() {
    setIsSimulating(true);
    setUploadError(null);

    try {
      // The real thing: create the package, hash each file in the browser, register it, PUT the
      // bytes straight to storage against the returned ticket, and confirm. The API never carries
      // the file — `src/api/upload.ts` explains why.
      const { reviewSessionId } = await createPackage(
        projectId(),
        { vendor, architectural: archFile, shop: shopFile },
        (progress: UploadProgress) => {
          setUploadStep(progress.file ? `${progress.step} ${progress.file}` : progress.step);
        },
      );

      setIsSimulating(false);
      setIsModalOpen(false);
      if (reviewSessionId) setActiveSession(reviewSessionId);
      setActivePage('review');
      setEvidencePanel(null);
      setArchFile(null);
      setShopFile(null);
    } catch (error) {
      // Left on screen with the modal open. Closing it and returning to the list would look exactly
      // like a successful upload, and the reviewer would go looking for a document that does not
      // exist. Nothing has been recorded — the API confirms bytes before it writes anything.
      setIsSimulating(false);
      setUploadError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <AppShell
      activePage={activePage}
      onNavigate={handleNavigate}
      activeSession={activeSession}
      onSelectSession={handleSelectSession}
      evidencePanel={evidencePanel}
      onNewPackage={() => setIsModalOpen(true)}
    >
      {activePage === 'review' && !activeSession && (
        <WelcomePage
          onStartSession={handleWelcomeStart}
          onSend={handleWelcomeSend}
          onNewPackage={() => setIsModalOpen(true)}
        />
      )}

      {activePage === 'review' && activeSession && (
        <ReviewPage
          key={activeSession}
          sessionId={activeSession}
          onEvidenceChange={setEvidencePanel}
          initialMessage={pendingMessage}
          onMessageConsumed={() => setPendingMessage('')}
        />
      )}

      {activePage === 'documents' && (
        <PackagesPage
          onOpenReview={handleOpenReview}
          onNewPackage={() => setIsModalOpen(true)}
        />
      )}

      {activePage === 'measure' && (
        <EnterValuesPage
          onDone={() => {
            // Straight to the packages list rather than to a findings view for this
            // package: the checks are asynchronous, so there may be nothing to show yet,
            // and a findings page that opened empty would read as a failed run.
            setActivePage('documents');
          }}
        />
      )}
      {activePage === 'confirm' && activeSession && (
        <ConfirmReadingsPage
          packageId={activeSession}
          onDone={() => {
            // The packages list, not a findings view: confirming a reading does not run the
            // checks, and a findings page opened straight afterwards would read as an empty result
            // rather than as work not yet asked for.
            setActivePage('documents');
          }}
        />
      )}

      {activePage === 'rulebook' && <RulebookPage />}

      {activePage === 'usage' && <UsagePage />}

      {/* ── NEW PACKAGE UPLOAD MODAL ───────────────────────── */}
      {isModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true">
          <div className="modal">
            <div className="modal__header">
              <span className="modal__title">
                {isSimulating ? 'Submitting document set' : 'Submit New Document'}
              </span>
              {!isSimulating && (
                <button
                  className="btn btn--subtle btn--icon btn--sm"
                  onClick={() => setIsModalOpen(false)}
                  aria-label="Close modal"
                >
                  <X size={15} />
                </button>
              )}
            </div>

            {uploadError && !isSimulating && (
              /* Shown with the modal still open. Closing it would look exactly like a successful
                 upload, and the reviewer would go looking for a document that does not exist.
                 Nothing has been recorded — the API confirms the bytes before it writes anything. */
              <div className="modal__body upload-error" role="alert">
                <strong>The document set was not submitted.</strong>
                <p>{uploadError}</p>
              </div>
            )}
            {isSimulating ? (
              /* The actual transfer state, supplied by the upload path — never a staged imitation. */
              <div className="modal__body pipeline-overlay">
                <Loader2 className="pipeline-loader" size={32} />
                <p className="pipeline-step-live">{uploadStep || 'Starting document submission…'}</p>
                <p className="pipeline-step__meta">The review opens when both uploaded PDFs are confirmed.</p>
              </div>
            ) : (
              /* Package Submission Form */
              <>
                <div className="modal__body">
                  <div className="form-row">
                    <div className="form-group">
                      <label className="form-group__label">Vendor Name</label>
                      <input
                        className="input"
                        value={vendor}
                        onChange={e => setVendor(e.target.value)}
                        placeholder="e.g. Apex Glass & Stone"
                      />
                    </div>
                </div>

                  <div className="form-group">
                    <label className="form-group__label">Upload Drawings (PDF only)</label>
                    <div className="upload-zone-wrapper">
                      {/* Real file inputs. The zones used to set a filename string on click, which
                          meant the pipeline had nothing to hash, upload or confirm. */}
                      <input
                        ref={archInputRef}
                        type="file"
                        accept="application/pdf"
                        hidden
                        onChange={(e) => setArchFile(e.target.files?.[0] ?? null)}
                      />
                      <input
                        ref={shopInputRef}
                        type="file"
                        accept="application/pdf"
                        hidden
                        onChange={(e) => setShopFile(e.target.files?.[0] ?? null)}
                      />

                      {/* Arch upload zone */}
                      <div
                        className={`upload-zone ${archFile?.name ? 'upload-zone--has-file' : ''}`}
                        onClick={() => archInputRef.current?.click()}

                        role="button"
                        aria-label="Upload Architectural Drawing Set"
                      >
                        <UploadCloud size={20} className="text-muted" />
                        <span className="upload-zone__title">Architectural Set</span>
                        {archFile?.name ? (
                          <span className="upload-zone__filename">{archFile?.name}</span>
                        ) : (
                          <span className="upload-zone__desc">Click to select PDF</span>
                        )}
                      </div>

                      {/* Shop drawing upload zone */}
                      <div
                        className={`upload-zone ${shopFile?.name ? 'upload-zone--has-file' : ''}`}
                        onClick={() => shopInputRef.current?.click()}
                        
                        role="button"
                        aria-label="Upload Shop Drawing Set"
                      >
                        <UploadCloud size={20} className="text-muted" />
                        <span className="upload-zone__title">Shop Drawings</span>
                        {shopFile?.name ? (
                          <span className="upload-zone__filename">{shopFile?.name}</span>
                        ) : (
                          <span className="upload-zone__desc">Click to select PDF</span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="modal__footer">
                  <button
                    className="btn btn--subtle"
                    onClick={() => setIsModalOpen(false)}
                  >
                    Cancel
                  </button>
                  <button
                    className="btn btn--action"
                    disabled={!vendor || !archFile?.name || !shopFile?.name}
                    onClick={triggerSubmitPipeline}
                  >
                    Run Review Pipeline
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </AppShell>
  );
}
