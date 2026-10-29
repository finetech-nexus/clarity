"use client";

import { FormEvent, useEffect, useState } from "react";
import DocumentViewer, { ViewerDocument } from "./DocumentViewer";
import { api, searchPage } from "../lib/api";

type QueueItem = {
  id: string;
  name: string;
  definition: string;
  definition_key?: string;
  status: string;
  state: string | null;
  documents: number;
  revisions: number;
  alert?: boolean;
};

type Field = { key: string; label: string; value: string; match?: boolean | null };

type OcrRead = {
  engine: string;
  confidence: number;
  production: boolean;
  extracted_text?: string;
};

type Detail = {
  case: QueueItem & { task?: { key: string; name: string } | null };
  task: { key: string; name: string } | null;
  customer: {
    display_name: string;
    status: string;
    risk_tier: string | null;
    synthetic: boolean;
    individual: Record<string, string | null> | null;
    business: Record<string, string | null> | null;
    screening: Array<{ id: string; status: string; production: boolean; matches: Array<{ id: string; status: string; score: number; name?: string }> }>;
    verification_checks: Array<Record<string, unknown>>;
    risk?: { score: number; tier: string; factors?: Array<{ code: string; points: number; explanation: string }> } | null;
  } | null;
  submitted: Field[];
  identity?: Field[];
  alert?: boolean;
  alert_reasons?: string[];
  documents: Array<ViewerDocument & { extracted?: Field[]; ocr?: OcrRead | null; decision?: string | null; status?: string; type?: string; category?: string }>;
  tracker: Array<{ id?: string; type: string; category: string; country?: string; required: boolean; status: string; document_id?: string | null; label?: { fr?: string; en?: string; ar?: string } }>;
  requested_documents?: Array<{ id: string; type: string; mode: string; reason?: string; label?: { fr?: string; en?: string; ar?: string } }>;
  face_match?: { score: number | null; match: boolean | null; threshold?: number; source?: string; detail?: string } | null;
  face_threshold?: number;
  steps?: Array<{ key: string; label: string; status: "done" | "current" | "upcoming" }>;
  notes?: Array<{ id: string; comment: string; actor?: string; created_at?: string; action?: string | null }>;
};

const TABS = ["Informations saisies", "Documents", "Données du document", "Reconnaissance faciale", "Criblage", "Notes", "Action"] as const;
const PREVIEW_TABS = ["Informations saisies", "Documents", "Données du document", "Reconnaissance faciale", "Score", "Criblage", "Notes", "Action"] as const;

const STEP_LABEL: Record<string, string> = {
  collect: "Collecte",
  verify: "Vérification",
  screen: "Criblage",
  score: "Évaluation du risque",
  analystReview: "Revue analyste",
  checkerReview: "Contrôle checker",
  documentsRequested: "Pièces demandées",
  remediate: "Complément",
  investigate: "Investigation",
  approved: "Approuvé",
  "approved-partially": "Approuvé partiellement",
  rejected: "Rejeté",
  closed: "Clôturé",
};

function facePercent(score: number | null | undefined) {
  if (score == null) return null;
  return Math.max(0, Math.min(100, Math.round(score <= 1 ? score * 100 : score)));
}

function FaceScale({ score, threshold }: { score: number | null | undefined; threshold: number }) {
  const value = facePercent(score);
  const cut = Math.max(0, Math.min(100, threshold));
  return (
    <div className="face-scale">
      <div className="face-track">
        <span className="face-mark" style={{ left: `${cut}%` }} />
        {value != null ? <span className="face-cursor" style={{ left: `${value}%` }}>{value}</span> : null}
      </div>
      <div className="face-axis">
        <span>0</span>
        <span className="face-threshold-label" style={{ left: `${cut}%` }}>{cut}</span>
        <span>100</span>
      </div>
    </div>
  );
}

function faceMatchLabel(match: boolean | null | undefined, comparing: boolean) {
  if (match == null) return comparing ? "Comparaison des visages…" : "Comparaison non faite";
  return match ? "Résultat de correspondance : OK" : "Résultat de correspondance : KO";
}

function formatNoteDate(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

function stepLabel(state: string | null | undefined, fallback: string) {
  if (!state) return fallback;
  return STEP_LABEL[state] || state;
}

const STATUS_FILTERS = [
  { value: "active", label: "En cours" },
  { value: "completed", label: "Terminée" },
  { value: "approved-partially", label: "Approuvé partiellement" },
  { value: "failed", label: "Échec" },
];

function statusLabel(status: string, state?: string | null) {
  if (status === "failed" || state === "rejected") return "Échec";
  if (status === "approved-partially" || state === "approved-partially") return "Approuvé partiellement";
  return STATUS_FILTERS.find((item) => item.value === status)?.label || stepLabel(state, status);
}

function filtersQuery(name: string, statuses: string[]) {
  const parts: string[] = [];
  const trimmed = name.trim().replaceAll('"', "");
  if (trimmed) parts.push(trimmed.includes(" ") || trimmed.includes(":") ? `name:"${trimmed}"` : `name:${trimmed}`);
  statuses.forEach((status) => parts.push(`status:${status}`));
  return parts.join(" ");
}

const TASK_ACTION: Record<string, string> = {
  analystReview: "Transmettre au contrôle",
  checkerReview: "Approuver",
  investigate: "Clôturer l'instruction",
};

const NOTE_ACTION_LABEL: Record<string, string> = {
  approve: "Transmission / approbation",
  reject: "Rejet",
  approvePartial: "Approbation partielle",
  "approve-partial": "Approbation partielle",
};

function NotesHistory({
  notes,
  freeNote,
  onFreeNote,
  onAddNote,
  canAdd,
}: {
  notes: Array<{ id: string; comment: string; actor?: string; created_at?: string; action?: string | null }>;
  freeNote: string;
  onFreeNote: (value: string) => void;
  onAddNote: () => void;
  canAdd: boolean;
}) {
  return (
    <article className="card notes-panel">
      <h3>Historique des notes</h3>
      {canAdd ? (
        <div className="note-compose">
          <textarea value={freeNote} onChange={(event) => onFreeNote(event.target.value)} placeholder="Écrire une note…" rows={3} />
          <div className="note-compose-actions">
            <button type="button" disabled={!freeNote.trim()} onClick={onAddNote}>Ajouter</button>
          </div>
        </div>
      ) : null}
      {notes.length ? (
        <ul className="notes-list">
          {[...notes].reverse().map((note) => (
            <li key={note.id}>
              <p className="note-meta">
                <strong>{note.actor || "Équipe"}</strong>
                {note.created_at ? <span>{formatNoteDate(note.created_at)}</span> : null}
                {note.action ? <span>{NOTE_ACTION_LABEL[note.action] || note.action}</span> : null}
              </p>
              <p className="note-body">{note.comment}</p>
            </li>
          ))}
        </ul>
      ) : <p className="label">Aucune note sur ce dossier.</p>}
    </article>
  );
}

function ActionTiles({
  reviewState,
  fundingReview,
  canDecide,
  canApprove,
  finished,
  rejectNote,
  approveNote,
  partialNote,
  onRejectNote,
  onApproveNote,
  onPartialNote,
  onDecide,
  resubmitId,
  complementTarget,
  askReason,
  sentDocuments,
  complements,
  onResubmitId,
  onComplementTarget,
  onAskReason,
  onRequestDocuments,
}: {
  reviewState: string;
  fundingReview: boolean;
  canDecide: boolean;
  canApprove: boolean;
  finished: boolean;
  rejectNote: string;
  approveNote: string;
  partialNote: string;
  onRejectNote: (value: string) => void;
  onApproveNote: (value: string) => void;
  onPartialNote: (value: string) => void;
  onDecide: (action: "approve" | "reject" | "approve-partial", note: string) => void;
  resubmitId: string;
  complementTarget: string;
  askReason: string;
  sentDocuments: Array<{ id: string; title?: string; type?: string }>;
  complements: Array<{ type: string; category: string; country?: string; label?: { fr?: string } }>;
  onResubmitId: (value: string) => void;
  onComplementTarget: (value: string) => void;
  onAskReason: (value: string) => void;
  onRequestDocuments: (event: FormEvent, mode: "resubmit" | "additional", target: string) => void;
}) {
  const isAnalyst = reviewState === "analystReview";
  const isChecker = reviewState === "checkerReview";
  const canPartial = Boolean(canDecide && isChecker && !fundingReview);
  const primaryLabel = TASK_ACTION[reviewState] || (isAnalyst ? "Transmettre au contrôle" : "Approuver");
  return (
    <div className="stack">
      {finished ? <p className="label">Aucune action ouverte sur ce dossier.</p> : (
        <div className="action-grid">
          {canDecide ? (
            <article className="card action-tile">
              <h3>Rejeter</h3>
              <div className="form">
                <textarea value={rejectNote} onChange={(event) => onRejectNote(event.target.value)} placeholder="Note de rejet" />
                <button className="ghost" type="button" onClick={() => onDecide("reject", rejectNote)}>Rejeter</button>
              </div>
            </article>
          ) : null}
          {canDecide && (isAnalyst || isChecker || reviewState === "investigate") ? (
            <article className="card action-tile">
              <h3>{isAnalyst ? "Transmettre" : isChecker ? "Approuver" : "Décision"}</h3>
              <div className="form">
                <textarea
                  value={approveNote}
                  onChange={(event) => onApproveNote(event.target.value)}
                  placeholder={isAnalyst ? "Note de transmission" : "Note d'approbation"}
                />
                <button type="button" disabled={!canApprove} onClick={() => onDecide("approve", approveNote)}>{primaryLabel}</button>
              </div>
            </article>
          ) : null}
          {canPartial ? (
            <article className="card action-tile">
              <h3>Approuver partiellement</h3>
              <div className="form">
                <textarea value={partialNote} onChange={(event) => onPartialNote(event.target.value)} placeholder="Note d'approbation partielle" />
                <button type="button" disabled={!canApprove} onClick={() => onDecide("approve-partial", partialNote)}>Approuver partiellement</button>
              </div>
            </article>
          ) : null}
          {canDecide ? (
            <>
              <article className="card action-tile">
                <h3>Demande de renvoi</h3>
                <form className="form" onSubmit={(event) => onRequestDocuments(event, "resubmit", resubmitId)}>
                  <label>
                    Pièce déjà envoyée
                    <select value={resubmitId} onChange={(event) => onResubmitId(event.target.value)} required>
                      <option value="">Choisir</option>
                      {sentDocuments.map((document) => (
                        <option key={document.id} value={document.id}>{document.title || document.type}</option>
                      ))}
                    </select>
                  </label>
                  <textarea value={askReason} onChange={(event) => onAskReason(event.target.value)} placeholder="Motif" />
                  <button type="submit">Demander le renvoi</button>
                </form>
              </article>
              <article className="card action-tile">
                <h3>Demander un complément</h3>
                <form className="form" onSubmit={(event) => onRequestDocuments(event, "additional", complementTarget)}>
                  <label>
                    Pièce complémentaire
                    <select value={complementTarget} onChange={(event) => onComplementTarget(event.target.value)} required>
                      <option value="">Choisir</option>
                      {complements.map((row) => (
                        <option key={`${row.type}-${row.category}`} value={`${row.type}|${row.category}|${row.country || "ZZ"}`}>
                          {row.label?.fr || row.type}
                        </option>
                      ))}
                    </select>
                  </label>
                  <textarea value={askReason} onChange={(event) => onAskReason(event.target.value)} placeholder="Motif" />
                  <button type="submit">Demander le complément</button>
                </form>
              </article>
            </>
          ) : null}
        </div>
      )}
    </div>
  );
}

export default function KycDesk({ kind }: { kind?: "business" }) {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("status:active");
  const [nameQuery, setNameQuery] = useState("");
  const [statuses, setStatuses] = useState<string[]>(["active"]);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Informations saisies");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reading, setReading] = useState(false);
  const [resubmitId, setResubmitId] = useState("");
  const [complementTarget, setComplementTarget] = useState("");
  const [askReason, setAskReason] = useState("");
  const [comparing, setComparing] = useState(false);
  const [faceTried, setFaceTried] = useState("");
  const [rejectNote, setRejectNote] = useState("");
  const [approveNote, setApproveNote] = useState("");
  const [partialNote, setPartialNote] = useState("");
  const [freeNote, setFreeNote] = useState("");

  async function loadQueue(prefer?: string, nextQuery = query, nextPage = page) {
    const body = await searchPage<QueueItem>(kind ? `/api/clarity/v1/desks/kyc?kind=${kind}` : "/api/clarity/v1/desks/kyc", nextQuery, nextPage);
    setQueue(body.items);
    setTotal(body.total);
    const requested = new URLSearchParams(window.location.search).get("case") || "";
    setSelected((current) => {
      if (prefer) return prefer;
      if (requested && body.items.some((row) => row.id === requested)) return requested;
      if (current && body.items.some((row) => row.id === current)) return current;
      return body.items[0]?.id || "";
    });
  }

  useEffect(() => {
    void loadQueue(undefined, query, page).catch((err: Error) => setError(err.message));
  }, [query, page]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    void api<Detail>(`/api/clarity/v1/desks/kyc/${selected}`).then(setDetail).catch((err: Error) => setError(err.message));
  }, [selected]);

  async function decideCase(action: "approve" | "reject" | "revision" | "approve-partial", note = "") {
    setError("");
    try {
      const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/decision`, {
        method: "POST",
        body: JSON.stringify({ action, rationale: note }),
      });
      setDetail(updated);
      setRejectNote("");
      setApproveNote("");
      setPartialNote("");
      setMessage(action === "revision" ? "Renvoi demandé. Le client doit fournir les pièces marquées." : "Décision enregistrée sur le dossier.");
      await loadQueue(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision refusée");
    }
  }

  async function addCaseNote() {
    if (!selected || !freeNote.trim()) return;
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/notes`, {
        method: "POST",
        body: JSON.stringify({ comment: freeNote }),
      }));
      setFreeNote("");
      setMessage("Note enregistrée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Note refusée");
    }
  }

  const identityKey = (detail?.identity || []).map((field) => `${field.key}:${field.value}`).join("|");

  useEffect(() => {
    if (!detail) return;
    const next: Record<string, string> = {};
    for (const field of detail.identity || []) next[field.key] = field.value;
    setDraft(next);
  }, [selected, identityKey]);

  async function readIdentity() {
    setError("");
    setReading(true);
    try {
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/identity/ocr`, { method: "POST" });
      setDetail(refreshed);
      const found = (refreshed.identity || []).some((field) => ["firstName", "lastName", "dateOfBirth"].includes(field.key) && field.value);
      setMessage(found ? "Lecture enregistrée." : "Aucune identité lue sur la carte ou le passeport.");
      await loadQueue(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Lecture refusée");
    } finally {
      setReading(false);
    }
  }

  async function saveIdentity(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/identity`, {
        method: "PATCH",
        body: JSON.stringify({ fields: draft }),
      }));
      setMessage("Données d'identité enregistrées.");
      await loadQueue(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function disposition(id: string, status: "no_hit" | "confirmed_hit") {
    setError("");
    try {
      await api(`/api/clarity/v1/screening/matches/${id}/disposition`, { method: "POST", body: JSON.stringify({ status }) });
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}`));
      setMessage(status === "no_hit" ? "Correspondance écartée." : "Correspondance confirmée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vérification du criblage refusée");
    }
  }

  async function requestDocuments(event: FormEvent, mode: "resubmit" | "additional", target: string) {
    event.preventDefault();
    if (!selected || !target) return;
    setError("");
    const item = mode === "resubmit"
      ? { document_id: target }
      : { type: target.split("|")[0], category: target.split("|")[1], country: target.split("|")[2] || "ZZ" };
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/document-requests`, {
        method: "POST",
        body: JSON.stringify({ reason: askReason, items: [item] }),
      }));
      setAskReason("");
      setResubmitId("");
      setComplementTarget("");
      setMessage(mode === "resubmit" ? "Renvoi demandé au client." : "Pièce complémentaire demandée au client.");
      await loadQueue(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demande refusée");
    }
  }

  async function compareFaces() {
    if (!selected) return;
    setError("");
    setComparing(true);
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/face-match`, { method: "POST" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Comparaison des visages refusée");
    } finally {
      setComparing(false);
    }
  }

  async function decideDocument(id: string, decision: "approve" | "reject" | "revision") {
    const updated = await api<ViewerDocument>(`/api/clarity/v1/desks/documents/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    setDetail((current) => current && ({
      ...current,
      documents: current.documents.map((item) => (item.id === id ? { ...item, ...updated } : item)),
    }));
  }

  const taskKey = detail?.task?.key || "";
  const matches = (detail?.customer?.screening || []).flatMap((item) => item.matches || []);
  const piecesApproved = Boolean(detail?.documents.length) && detail!.documents.every((item) => item.decision === "approved");
  const screeningPending = matches.some((item) => item.status === "pending");
  const fundingReview = detail?.case.definition_key === "clarityFundingOrigin";
  const reviewState = taskKey || detail?.case.state || "";
  const canDecide = reviewState === "analystReview" || reviewState === "checkerReview" || reviewState === "investigate";
  const canApprove = canDecide && (fundingReview || piecesApproved) && !screeningPending;
  const finished = !detail || detail.case.status === "failed" || detail.case.state === "approved" || detail.case.state === "approved-partially" || detail.case.state === "rejected" || detail.case.state === "closed";
  const sentDocuments = (detail?.documents || []).filter((document) => document.status !== "requested");
  const complements = (detail?.tracker || []).filter((row) => !row.document_id);
  const screeningFactors = (detail?.customer?.risk?.factors || []).filter((factor) => factor.code === "screening_confirmed");
  const otherFactors = (detail?.customer?.risk?.factors || []).filter((factor) => factor.code !== "screening_confirmed");

  useEffect(() => {
    setFaceTried("");
  }, [selected]);

  useEffect(() => {
    if (tab !== "Reconnaissance faciale" || !selected || !detail || detail.face_match || faceTried === selected || comparing) return;
    setFaceTried(selected);
    void compareFaces();
  }, [tab, selected, detail, faceTried, comparing]);

  return (
    <>
      <h1>Vérification</h1>
      <p className="lede">Dossier de vérification : informations saisies, pièces et données lues sur les documents.</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <div className="filters">
        <label className="field">Nom
          <input
            aria-label="Rechercher par nom"
            placeholder="Rechercher"
            value={nameQuery}
            onChange={(event) => {
              const next = event.target.value;
              setNameQuery(next);
              setPage(0);
              setQuery(filtersQuery(next, statuses));
            }}
          />
        </label>
        <div className="checks" role="group" aria-label="Statut">
          {STATUS_FILTERS.map((item) => (
            <label key={item.value}>
              <input
                type="checkbox"
                checked={statuses.includes(item.value)}
                onChange={() => {
                  const next = statuses.includes(item.value) ? statuses.filter((value) => value !== item.value) : [...statuses, item.value];
                  setStatuses(next);
                  setPage(0);
                  setQuery(filtersQuery(nameQuery, next));
                }}
              />
              {item.label}
            </label>
          ))}
        </div>
      </div>
      <p className="label">{total} dossier(s) · page {page + 1}</p>
      <div className="tabs">
        <button className="ghost" type="button" disabled={page === 0} onClick={() => setPage((current) => Math.max(0, current - 1))}>Précédent</button>
        <button className="ghost" type="button" disabled={(page + 1) * 25 >= total} onClick={() => setPage((current) => current + 1)}>Suivant</button>
      </div>
      <div className="kyc-shell">
        <div className="queue">
          {queue.map((item) => (
            <button className={`${item.id === selected ? "active" : "ghost"}${item.alert ? " alert" : ""}`} key={item.id} type="button" onClick={() => setSelected(item.id)}>
              <span>{item.name}</span>
              <span className="label">{item.status === "active" ? stepLabel(item.state, item.status) : statusLabel(item.status, item.state)} · {item.documents} pièce(s){item.revisions ? ` · ${item.revisions} à renvoyer` : ""}</span>
            </button>
          ))}
        </div>
        {detail ? (
          <div className={`case-sheet${detail.alert ? " alert" : ""}`}>
            <header className="case-bar">
              <div>
                <h2>{detail.customer?.display_name || detail.case.name}</h2>
                <p className="label">{detail.case.definition} · {statusLabel(detail.case.status, detail.case.state)} · {detail.task?.name || stepLabel(detail.case.state, "sans tâche ouverte")}</p>
                {detail.steps?.length ? (
                  <ol className="stepper">
                    {detail.steps.map((step) => (
                      <li className={step.status} key={step.key}><span className="dot" />{step.label}</li>
                    ))}
                  </ol>
                ) : null}
                {otherFactors.length ? (
                  <ul className="factors">
                    {otherFactors.map((factor) => (
                      <li key={factor.code}>{factor.points} pts — {factor.explanation}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <div className="score-chip">
                <span className="label">Score de risque</span>
                <strong>{detail.customer?.risk ? detail.customer.risk.score : "—"}</strong>
                <span className="label">{detail.customer?.risk?.tier || "non calculé"}</span>
              </div>
            </header>
            <div className="case-layout">
            <div className="tabs case-tabs">
              {TABS.map((item) => (
                <button className={tab === item ? "active" : ""} key={item} type="button" onClick={() => setTab(item)}>{item}</button>
              ))}
            </div>
            <div className="case-pane">
            {tab === "Informations saisies" ? (
              <article className="card">
                {detail.submitted?.length ? (
                  <dl className="kv">
                    {detail.submitted.map((field) => (
                      <span key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></span>
                    ))}
                  </dl>
                ) : <p className="label">Aucune information saisie sur ce dossier.</p>}
              </article>
            ) : null}
            {tab === "Documents" ? (
              <div className="document-data">
                <h3>Liste des pièces</h3>
                {!detail.documents.length ? <p className="label">Aucune pièce fournie.</p> : null}
                <DocumentViewer documents={detail.documents} onDecide={(id, decision) => void decideDocument(id, decision)} />
              </div>
            ) : null}
            {tab === "Données du document" ? (
              <article className="card">
                {detail.identity?.length ? (
                  <form className="form" onSubmit={(event) => void saveIdentity(event)}>
                    {detail.identity.map((field) => (
                      <label className={field.match === false ? "mismatch" : ""} key={field.key}>
                        {field.label}
                        <input
                          value={draft[field.key] ?? field.value}
                          onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
                        />
                      </label>
                    ))}
                    <div className="actions">
                      <button className="ghost" type="button" disabled={reading} onClick={() => void readIdentity()}>
                        {reading ? "Lecture…" : "Lire les pièces"}
                      </button>
                      <button type="submit">Enregistrer</button>
                    </div>
                  </form>
                ) : <p className="label">Aucune carte d'identité ou passeport sur ce dossier.</p>}
              </article>
            ) : null}
            {tab === "Reconnaissance faciale" ? (
              <article className="card">
                <FaceScale score={detail.face_match?.score} threshold={detail.face_threshold ?? detail.face_match?.threshold ?? 40} />
                <p>{faceMatchLabel(detail.face_match?.match, comparing)}</p>
                <p className="label">{detail.face_match?.source === "video" ? "Visage extrait de la vidéo, comparé à la photo de la pièce." : "Selfie comparé à la photo de la pièce."}</p>
                {detail.face_match?.detail ? <p className="label">{detail.face_match.detail}</p> : null}
                <button type="button" disabled={comparing} onClick={() => void compareFaces()}>Comparer à nouveau</button>
              </article>
            ) : null}
            {tab === "Criblage" ? (
              <article className="card">
                {screeningFactors.map((factor) => (
                  <p className="factor" key={factor.code}>{factor.points} pts — {factor.explanation}</p>
                ))}
                {matches.length ? matches.map((match) => (
                  <p className="file-row" key={match.id}>
                    <span>{match.name || "Correspondance"} · score {match.score} · {match.status}</span>
                    {match.status === "pending" ? (
                      <span className="tabs">
                        <button type="button" onClick={() => void disposition(match.id, "no_hit")}>Faux positif</button>
                        <button className="ghost" type="button" onClick={() => void disposition(match.id, "confirmed_hit")}>Confirmer</button>
                      </span>
                    ) : <span className="tag">{match.status === "no_hit" ? "Écartée" : "Confirmée"}</span>}
                  </p>
                )) : <p className="label">Aucune correspondance sur ce dossier.</p>}
              </article>
            ) : null}
            {tab === "Notes" ? (
              <NotesHistory
                notes={detail.notes || []}
                freeNote={freeNote}
                onFreeNote={setFreeNote}
                onAddNote={() => void addCaseNote()}
                canAdd={!finished}
              />
            ) : null}
            {tab === "Action" ? (
              <div className="stack">
                {detail.alert_reasons?.includes("correspondence") ? <p className="alert-line">Les données lues sur la pièce ne correspondent pas aux informations saisies.</p> : null}
                {detail.alert_reasons?.includes("screening") ? <p className="alert-line">Le criblage remonte une alerte.</p> : null}
                {fundingReview && !finished ? <p className="label">Virement reçu sur un compte limité. Vérifiez la provenance avant d'activer le compte.</p> : null}
                {canDecide && !piecesApproved && !fundingReview && !finished ? <p className="error">Approuvez chaque pièce pour activer la validation.</p> : null}
                {canDecide && screeningPending && !finished ? <p className="label">Vérifiez chaque correspondance de criblage avant de valider.</p> : null}
                <ActionTiles
                  reviewState={reviewState}
                  fundingReview={Boolean(fundingReview)}
                  canDecide={Boolean(canDecide)}
                  canApprove={Boolean(canApprove)}
                  finished={Boolean(finished)}
                  rejectNote={rejectNote}
                  approveNote={approveNote}
                  partialNote={partialNote}
                  onRejectNote={setRejectNote}
                  onApproveNote={setApproveNote}
                  onPartialNote={setPartialNote}
                  onDecide={(action, note) => void decideCase(action, note)}
                  resubmitId={resubmitId}
                  complementTarget={complementTarget}
                  askReason={askReason}
                  sentDocuments={sentDocuments}
                  complements={complements}
                  onResubmitId={setResubmitId}
                  onComplementTarget={setComplementTarget}
                  onAskReason={setAskReason}
                  onRequestDocuments={(event, mode, target) => void requestDocuments(event, mode, target)}
                />
                {detail.requested_documents?.length ? (
                  <ul className="factors">
                    {detail.requested_documents.map((item) => (
                      <li key={item.id}>{item.label?.fr || item.type} — {item.mode === "resubmit" ? "à renvoyer" : "pièce complémentaire"}{item.reason ? ` · ${item.reason}` : ""}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
            </div>
            </div>
          </div>
        ) : selected ? <p>Chargement du dossier…</p> : null}
      </div>
    </>
  );
}

export function VerificationPreview({ runtimeId, onChanged }: { runtimeId: string; onChanged?: () => void }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [tab, setTab] = useState<(typeof PREVIEW_TABS)[number]>("Informations saisies");
  const [comparing, setComparing] = useState(false);
  const [faceTried, setFaceTried] = useState("");
  const [resubmitId, setResubmitId] = useState("");
  const [complementTarget, setComplementTarget] = useState("");
  const [askReason, setAskReason] = useState("");
  const [rejectNote, setRejectNote] = useState("");
  const [approveNote, setApproveNote] = useState("");
  const [partialNote, setPartialNote] = useState("");
  const [freeNote, setFreeNote] = useState("");

  useEffect(() => {
    setDetail(null);
    setError("");
    setMessage("");
    setTab("Informations saisies");
    setFaceTried("");
    setResubmitId("");
    setComplementTarget("");
    setAskReason("");
    setRejectNote("");
    setApproveNote("");
    setPartialNote("");
    setFreeNote("");
    void api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`).then(setDetail).catch((err: Error) => setError(err.message));
  }, [runtimeId]);

  async function compareFaces() {
    setError("");
    setComparing(true);
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/face-match`, { method: "POST" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Comparaison des visages refusée");
    } finally {
      setComparing(false);
    }
  }

  useEffect(() => {
    if (tab !== "Reconnaissance faciale" || !detail || detail.face_match || faceTried === runtimeId || comparing) return;
    setFaceTried(runtimeId);
    void compareFaces();
  }, [tab, detail, faceTried, comparing, runtimeId]);

  async function requestDocuments(event: FormEvent, mode: "resubmit" | "additional", target: string) {
    event.preventDefault();
    if (!target) return;
    setError("");
    const item = mode === "resubmit"
      ? { document_id: target }
      : { type: target.split("|")[0], category: target.split("|")[1], country: target.split("|")[2] || "ZZ" };
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/document-requests`, {
        method: "POST",
        body: JSON.stringify({ reason: askReason, items: [item] }),
      }));
      setAskReason("");
      setResubmitId("");
      setComplementTarget("");
      setMessage(mode === "resubmit" ? "Renvoi demandé au client." : "Pièce complémentaire demandée au client.");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demande refusée");
    }
  }

  async function decideCase(action: "approve" | "reject" | "approve-partial", note = "") {
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/decision`, {
        method: "POST",
        body: JSON.stringify({ action, rationale: note }),
      }));
      setRejectNote("");
      setApproveNote("");
      setPartialNote("");
      setMessage(action === "approve-partial" ? "Approbation partielle enregistrée." : "Décision enregistrée sur le dossier.");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision refusée");
    }
  }

  async function addCaseNote() {
    if (!freeNote.trim()) return;
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/notes`, {
        method: "POST",
        body: JSON.stringify({ comment: freeNote }),
      }));
      setFreeNote("");
      setMessage("Note enregistrée.");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Note refusée");
    }
  }

  if (error && !detail) return <p className="error">{error}</p>;
  if (!detail) return <p className="label">Chargement de la vérification…</p>;
  const matches = (detail.customer?.screening || []).flatMap((item) => item.matches || []);
  const taskKey = detail.task?.key || "";
  const fundingReview = detail.case.definition_key === "clarityFundingOrigin";
  const reviewState = taskKey || detail.case.state || "";
  const piecesApproved = Boolean(detail.documents.length) && detail.documents.every((item) => item.decision === "approved");
  const screeningPending = matches.some((item) => item.status === "pending");
  const canDecide = reviewState === "analystReview" || reviewState === "checkerReview" || reviewState === "investigate";
  const canApprove = canDecide && (fundingReview || piecesApproved) && !screeningPending;
  const finished = detail.case.status === "failed" || detail.case.state === "approved" || detail.case.state === "approved-partially" || detail.case.state === "rejected" || detail.case.state === "closed";
  const sentDocuments = (detail.documents || []).filter((document) => document.status !== "requested");
  const complements = (detail.tracker || []).filter((row) => !row.document_id);
  return (
    <div className="verify-preview">
      <aside className="verify-side">
        <h3>{detail.customer?.display_name || detail.case.name}</h3>
        <div className="tabs case-tabs">
          {PREVIEW_TABS.map((item) => (
            <button className={tab === item ? "active" : ""} key={item} type="button" onClick={() => setTab(item)}>{item}</button>
          ))}
        </div>
      </aside>
      <div>
        {error ? <p className="error">{error}</p> : null}
        {message ? <p className="label">{message}</p> : null}
        {tab === "Informations saisies" ? (
          <article className="card">
            {detail.submitted?.length ? (
              <dl className="kv">
                {detail.submitted.map((field) => (
                  <span key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></span>
                ))}
              </dl>
            ) : <p className="label">Aucune information saisie sur ce dossier.</p>}
          </article>
        ) : null}
        {tab === "Documents" ? (
          <DocumentViewer documents={detail.documents} locked onDecide={() => undefined} />
        ) : null}
        {tab === "Données du document" ? (
          <article className="card">
            {detail.identity?.length ? (
              <form className="form">
                {detail.identity.map((field) => (
                  <label key={field.key}>
                    {field.label}
                    <input disabled readOnly value={field.value} />
                  </label>
                ))}
                <div className="actions">
                  <button className="ghost" type="button" disabled>Lire les pièces</button>
                  <button type="button" disabled>Enregistrer</button>
                </div>
              </form>
            ) : <p className="label">Aucune carte d'identité ou passeport sur ce dossier.</p>}
          </article>
        ) : null}
        {tab === "Reconnaissance faciale" ? (
          <article className="card">
            <FaceScale score={detail.face_match?.score} threshold={detail.face_threshold ?? detail.face_match?.threshold ?? 40} />
            <p>{faceMatchLabel(detail.face_match?.match, comparing)}</p>
            <p className="label">{detail.face_match?.source === "video" ? "Visage extrait de la vidéo, comparé à la photo de la pièce." : "Selfie comparé à la photo de la pièce."}</p>
            {detail.face_match?.detail ? <p className="label">{detail.face_match.detail}</p> : null}
            <button type="button" disabled={comparing} onClick={() => void compareFaces()}>Comparer à nouveau</button>
          </article>
        ) : null}
        {tab === "Score" ? (
          <article className="card">
            <div className="metric">{detail.customer?.risk ? detail.customer.risk.score : "—"}</div>
            <p>{detail.customer?.risk?.tier || "Score non calculé"}</p>
            {(detail.customer?.risk?.factors || []).map((factor) => (
              <p className="factor" key={factor.code}>{factor.points} pts — {factor.explanation}</p>
            ))}
          </article>
        ) : null}
        {tab === "Criblage" ? (
          <article className="card">
            {(detail.customer?.risk?.factors || []).filter((factor) => factor.code === "screening_confirmed").map((factor) => (
              <p className="factor" key={factor.code}>{factor.points} pts — {factor.explanation}</p>
            ))}
            {matches.length ? matches.map((match) => (
              <p className="file-row" key={match.id}>
                <span>{match.name || "Correspondance"} · score {match.score} · {match.status}</span>
                <span className="tabs">
                  <button type="button" disabled>Faux positif</button>
                  <button className="ghost" type="button" disabled>Confirmer</button>
                </span>
              </p>
            )) : <p className="label">Aucune correspondance sur ce dossier.</p>}
          </article>
        ) : null}
        {tab === "Notes" ? (
          <NotesHistory
            notes={detail.notes || []}
            freeNote={freeNote}
            onFreeNote={setFreeNote}
            onAddNote={() => void addCaseNote()}
            canAdd={!finished}
          />
        ) : null}
        {tab === "Action" ? (
          <div className="stack">
            {fundingReview && !finished ? <p className="label">Virement reçu sur un compte limité. Vérifiez la provenance avant d'activer le compte.</p> : null}
            {canDecide && !piecesApproved && !fundingReview && !finished ? <p className="error">Approuvez chaque pièce pour activer la validation.</p> : null}
            <ActionTiles
              reviewState={reviewState}
              fundingReview={Boolean(fundingReview)}
              canDecide={Boolean(canDecide)}
              canApprove={Boolean(canApprove)}
              finished={Boolean(finished)}
              rejectNote={rejectNote}
              approveNote={approveNote}
              partialNote={partialNote}
              onRejectNote={setRejectNote}
              onApproveNote={setApproveNote}
              onPartialNote={setPartialNote}
              onDecide={(action, note) => void decideCase(action, note)}
              resubmitId={resubmitId}
              complementTarget={complementTarget}
              askReason={askReason}
              sentDocuments={sentDocuments}
              complements={complements}
              onResubmitId={setResubmitId}
              onComplementTarget={setComplementTarget}
              onAskReason={setAskReason}
              onRequestDocuments={(event, mode, target) => void requestDocuments(event, mode, target)}
            />
            {detail.requested_documents?.length ? (
              <ul className="factors">
                {detail.requested_documents.map((item) => (
                  <li key={item.id}>{item.label?.fr || item.type} — {item.mode === "resubmit" ? "à renvoyer" : "pièce complémentaire"}{item.reason ? ` · ${item.reason}` : ""}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
