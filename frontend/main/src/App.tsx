import { useState, useEffect, useRef } from 'react';
import { AppShell } from './components/shell/AppShell';
import { ReviewPage } from './pages/ReviewPage';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { createPackage } from './api/upload';
import type { UploadProgress } from './api/upload';
import { projectId } from './api/config';
import { X, UploadCloud, Loader2, CheckCircle2 } from 'lucide-react';
import './design/components.css';

const SIMULATION_STEPS = [
  { label: 'Uploading drawings to S3 vault...', meta: 'Generating SHA-256...' },
  { label: 'Registering metadata in PostgreSQL...', meta: 'Triggering Hatchet outbox event...' },
  { label: 'Running class check & PaddleOCR layout analysis...', meta: 'PaddleOCR + docTR active...' },
  { label: 'Extracting digital vector lines with pdfplumber...', meta: 'Parallel lanes running...' },
  { label: 'Normalizing shapes & loading rulebook snapshot...', meta: 'Resolving CT-v1.2 active rules...' },
  { label: 'Evaluating operands inside isolated Verdict engine...', meta: 'Exact Fraction math sealed...' }
];

export default function App() {
  const [activePage, setActivePage] = useState<string>('review');
  const [activeSession, setActiveSession] = useState<string>('');
  const [evidencePanel, setEvidencePanel] = useState<React.ReactNode>(null);
  const [pendingMessage, setPendingMessage] = useState<string>('');

  const [designStyle, setDesignStyle] = useState<'stone' | 'ide'>(() => {
    const saved = localStorage.getItem('gv-design-style');
    if (saved === 'ide') return 'ide';
    return 'stone';
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-style', designStyle);
    localStorage.setItem('gv-design-style', designStyle);
  }, [designStyle]);

  // New package form states
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [vendor, setVendor] = useState('Apex Glass & Stone');
  const [project, setProject] = useState('Westin Towers — Double Countertops');
  const [category, setCategory] = useState<'countertop' | 'cabinet'>('countertop');
  const [rulebook, setRulebook] = useState('CT-v1.2');
  const [archFile, setArchFile] = useState<File | null>(null);
  const [shopFile, setShopFile] = useState<File | null>(null);
  const archInputRef = useRef<HTMLInputElement>(null);
  const shopInputRef = useRef<HTMLInputElement>(null);
  const [uploadStep, setUploadStep] = useState('');
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Simulation steps states
  const [isSimulating, setIsSimulating] = useState(false);
  const [simulationStep, setSimulationStep] = useState(0);

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
    setSimulationStep(0);

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
      designStyle={designStyle}
      onStyleChange={setDesignStyle}
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

      {activePage === 'rulebook' && <RulebookPage />}

      {activePage === 'usage' && <UsagePage />}

      {/* ── NEW PACKAGE UPLOAD MODAL ───────────────────────── */}
      {isModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true">
          <div className="modal">
            <div className="modal__header">
              <span className="modal__title">
                {isSimulating ? 'Processing Ingestion Pipeline' : 'Submit New Document'}
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
              /* Simulated Pipeline status screen */
              <div className="modal__body pipeline-overlay">
                <Loader2 className="pipeline-loader" size={32} />
                {/* What is actually happening, rather than a fixed script. The old version stepped
                    through a timed list whatever the server was doing; this says which file is
                    being hashed, uploaded or confirmed. */}
                <p className="pipeline-step-live">{uploadStep || 'Starting…'}</p>
                <div className="pipeline-steps">
                  {SIMULATION_STEPS.map((step, idx) => {
                    const isCompleted = idx < simulationStep;
                    const isActive = idx === simulationStep;
                    return (
                      <div
                        key={idx}
                        className={`pipeline-step ${isCompleted ? 'pipeline-step--completed' : ''} ${isActive ? 'pipeline-step--active' : ''}`}
                      >
                        <div className="pipeline-step__status">
                          {isCompleted ? (
                            <CheckCircle2 size={15} />
                          ) : isActive ? (
                            <Loader2 size={13} className="animate-spin" />
                          ) : (
                            <div style={{ width: 13, height: 13, border: '1px solid var(--border-default)', borderRadius: '50%' }} />
                          )}
                        </div>
                        <span className="pipeline-step__label">{step.label}</span>
                        <span className="pipeline-step__meta">{isCompleted ? 'Done' : isActive ? step.meta : 'Waiting'}</span>
                      </div>
                    );
                  })}
                </div>
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
                    <div className="form-group">
                      <label className="form-group__label">Rulebook snapshot</label>
                      <select
                        className="input"
                        value={rulebook}
                        onChange={e => setRulebook(e.target.value)}
                      >
                        <option value="CT-v1.2">Countertop CT-v1.2</option>
                        <option value="CAB-v1.0">Cabinet CAB-v1.0</option>
                      </select>
                    </div>
                  </div>

                  <div className="form-group">
                    <label className="form-group__label">Project Context</label>
                    <input
                      className="input"
                      value={project}
                      onChange={e => setProject(e.target.value)}
                      placeholder="e.g. Westin Towers — Vanity Counters"
                    />
                  </div>

                  <div className="form-row">
                    <div className="form-group">
                      <label className="form-group__label">Category Scope</label>
                      <select
                        className="input"
                        value={category}
                        onChange={e => setCategory(e.target.value as any)}
                      >
                        <option value="countertop">Countertop</option>
                        <option value="cabinet">Cabinet</option>
                      </select>
                    </div>
                    <div className="form-group">
                      <label className="form-group__label">Upload Target Environment</label>
                      <input
                        className="input"
                        disabled
                        value="Immutable S3 (gv-vault)"
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
                    disabled={!vendor || !project || !archFile?.name || !shopFile?.name}
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
