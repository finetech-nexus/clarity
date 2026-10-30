"use client";

import { CSSProperties, FormEvent, useEffect, useState } from "react";
import DocumentViewer, { ViewerDocument } from "./DocumentViewer";
import { api, searchPage } from "../lib/api";
import { useI18n } from "../lib/i18n";

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

type TopicDetail = { key?: string; label: string; points?: number; tier?: "critical" | "elevated" | "info" | string };

type ScreeningMatch = {
  id: string;
  status: string;
  score: number;
  name?: string;
  topics?: string[];
  topic_details?: TopicDetail[];
  datasets?: string[];
  note?: string;
  explanation?: string[] | string;
  provider?: string;
  synthetic?: boolean;
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
    screening: Array<{ id: string; status: string; production: boolean; matches: ScreeningMatch[] }>;
    verification_checks: Array<Record<string, unknown>>;
    risk?: {
      score: number;
      tier: string;
      factors?: Array<{ code: string; points: number; explanation: string }>;
      formula_note?: string;
      methodology_version?: string;
    } | null;
  } | null;
  submitted: Field[];
  transfer?: {
    id?: string | null;
    amount?: number | string | null;
    currency?: string | null;
    counterparty_name?: string | null;
    direction?: string | null;
    description?: string | null;
    payment_reference?: string | null;
    transaction_reference?: string | null;
    account_id?: string | null;
    channel?: string | null;
    transaction_type?: string | null;
    country?: string | null;
    occurred_at?: string | null;
    fields?: Field[];
  } | null;
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

const DEFAULT_FORMULA_NOTE =
  "Score = somme des points AML + visage + authenticité des pièces + historique d'opérations + pays/structure. "
  + "AML : chaque topic a une pondération distincte (Sanctioned / Wanted / Export controlled élevés ; "
  + "Debarred / Disqualified / PEP moyens ; Person of interest / Politician informatifs). "
  + "Plusieurs topics se cumulent. Un hit sanctions reste élevé même en candidat. "
  + "Seuils : <30 faible, 30–59 moyen, ≥60 élevé.";

const TOPIC_TIER_FALLBACK: Record<string, TopicDetail["tier"]> = {
  "Export controlled": "critical",
  Sanctioned: "critical",
  Wanted: "critical",
  Debarred: "elevated",
  Disqualified: "elevated",
  PEP: "elevated",
  "Person of interest": "info",
  Politician: "info",
};

function topicRows(match: ScreeningMatch): TopicDetail[] {
  if (match.topic_details?.length) return match.topic_details;
  return (match.topics || []).map((label) => ({
    label,
    tier: TOPIC_TIER_FALLBACK[label] || "info",
  }));
}

function TopicChips({ topics }: { topics: TopicDetail[] }) {
  if (!topics.length) return <span className="label">—</span>;
  return (
    <div className="topic-chips" aria-label="Topics">
      {topics.map((topic) => (
        <span className={`topic-chip topic-${topic.tier || "info"}`} key={`${topic.label}-${topic.key || ""}`}>
          {topic.label}
          {topic.points != null ? <em>{topic.points}</em> : null}
        </span>
      ))}
    </div>
  );
}

const TAB_ALERT_REASONS: Record<string, string[]> = {
  "Données du document": ["correspondence"],
  Documents: ["document"],
  "Reconnaissance faciale": ["face"],
  Criblage: ["screening"],
};

function tabHasAlert(tab: string, reasons: string[] | undefined) {
  const keys = TAB_ALERT_REASONS[tab] || [];
  return keys.some((key) => reasons?.includes(key));
}

const TABS = ["Informations saisies", "Documents", "Données du document", "Reconnaissance faciale", "Criblage", "Notes", "Action"] as const;
const PREVIEW_TABS = ["Informations saisies", "Documents", "Données du document", "Reconnaissance faciale", "Criblage", "Notes", "Action"] as const;

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
        <span className="face-threshold-label" style={{ left: `${cut}%` }}>seuil {cut}</span>
        <span>100</span>
      </div>
    </div>
  );
}

function faceMatchLabel(match: boolean | null | undefined, comparing: boolean) {
  if (match == null) return comparing ? "Comparaison des visages…" : "Comparaison non faite";
  return match ? "Résultat de correspondance : OK" : "Résultat de correspondance : KO";
}

function ScoreFormulaHint({ note }: { note?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="formula-hint" onClick={(event) => event.stopPropagation()}>
      <button
        className="formula-q"
        type="button"
        aria-label="Formule du score de risque"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((current) => !current);
        }}
      >
        ?
      </button>
      {open ? <span className="formula-note" role="note">{note || DEFAULT_FORMULA_NOTE}</span> : null}
    </span>
  );
}

function mixChannel(from: number, to: number, t: number) {
  return Math.round(from + (to - from) * t);
}

function riskScoreColor(score: number | null | undefined) {
  if (score == null || Number.isNaN(score)) return null;
  const value = Math.max(0, Math.min(100, Number(score)));
  // 0 → green, 50 → yellow, 100 → red (no blue/teal).
  const low: [number, number, number] = [52, 168, 72];
  const mid: [number, number, number] = [232, 176, 48];
  const high: [number, number, number] = [201, 76, 83];
  const [from, to, t] = value <= 50
    ? [low, mid, value / 50]
    : [mid, high, (value - 50) / 50];
  const rgb = [
    mixChannel(from[0], to[0], t),
    mixChannel(from[1], to[1], t),
    mixChannel(from[2], to[2], t),
  ];
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

function riskScoreChipStyle(score: number | null | undefined): CSSProperties | undefined {
  const color = riskScoreColor(score);
  if (!color) return undefined;
  return {
    color,
    borderColor: color,
    background: `color-mix(in srgb, ${color} 14%, white)`,
  };
}

export function RiskScoreChip({
  score,
  tier,
  note,
  factors,
  metric,
  labels,
}: {
  score?: number | null;
  tier?: string | null;
  note?: string;
  factors?: Array<{ code: string; points: number; explanation: string }>;
  metric?: boolean;
  labels?: { score: string; breakdown: string; empty: string; click: string };
}) {
  const [open, setOpen] = useState(false);
  const value = score == null ? "—" : score;
  const copy = {
    score: labels?.score || "Score de risque",
    breakdown: labels?.breakdown || "Détail du calcul",
    empty: labels?.empty || "Score non calculé",
    click: labels?.click || "Cliquer pour voir le détail du calcul",
  };
  return (
    <div className={`score-chip${open ? " is-open" : ""}${metric ? " is-metric" : ""}`} style={riskScoreChipStyle(score)}>
      <button
        className="score-chip-toggle"
        type="button"
        aria-expanded={open}
        aria-label={`${copy.score}: ${value}. ${copy.click}`}
        title={copy.click}
        onClick={() => setOpen((current) => !current)}
      >
        <strong className={metric ? "metric" : undefined}>{value}</strong>
        <span className="score-chip-tier">{tier || copy.empty}</span>
      </button>
      {open ? (
        <div className="score-breakdown" role="region" aria-label={copy.breakdown}>
          <p className="label">{copy.breakdown} <ScoreFormulaHint note={note} /></p>
          {factors?.length ? (
            <ul className="factors">
              {factors.map((factor) => (
                <li key={factor.code}>{factor.points} pts — {factor.explanation}</li>
              ))}
            </ul>
          ) : (
            <p className="label">{copy.empty}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function ScreeningMatchRow({
  match,
  pendingActions,
}: {
  match: ScreeningMatch;
  pendingActions?: { onClear: () => void; onConfirm: () => void } | null;
}) {
  const topics = topicRows(match);
  const datasets = match.datasets?.length ? match.datasets.join(", ") : "";
  const explanation = Array.isArray(match.explanation) ? match.explanation.join(" ") : match.explanation || "";
  return (
    <div className="screening-match">
      <p className="file-row">
        <span>
          <strong>{match.name || "Correspondance"}</strong>
          {" · score "}{match.score}
          {" · "}{match.status}
        </span>
        {match.status === "pending" && pendingActions ? (
          <span className="tabs">
            <button type="button" onClick={pendingActions.onClear}>Faux positif</button>
            <button className="ghost" type="button" onClick={pendingActions.onConfirm}>Confirmer</button>
          </span>
        ) : (
          <span className="tag">{match.status === "no_hit" ? "Écartée" : match.status === "confirmed_hit" ? "Confirmée" : match.status}</span>
        )}
      </p>
      <div className="screening-topics">
        <span className="label">Topics</span>
        <TopicChips topics={topics} />
      </div>
      <dl className="kv screening-detail">
        {datasets ? <span><dt>Jeux</dt><dd>{datasets}</dd></span> : null}
        {match.note ? <span><dt>Détail</dt><dd>{match.note}</dd></span> : null}
        {explanation ? <span><dt>Explication</dt><dd>{explanation}</dd></span> : null}
        {match.provider ? <span><dt>Source</dt><dd>{match.provider}{match.synthetic ? " · synthétique" : ""}</dd></span> : null}
      </dl>
    </div>
  );
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

const ACTIVATE_TASK_ACTION: Record<string, string> = {
  analystReview: "Transmettre pour activation",
  checkerReview: "Approuver le KYC",
};

function TransferDetail({ transfer, submitted }: { transfer?: Detail["transfer"]; submitted: Field[] }) {
  const fields = transfer?.fields?.length ? transfer.fields : submitted.filter((field) =>
    ["amount", "currency", "counterpartyName", "direction", "description", "paymentReference", "transactionReference", "accountId", "channel", "transactionType", "occurredAt", "country"].includes(field.key)
  );
  const profile = submitted.filter((field) => !fields.some((row) => row.key === field.key));
  return (
    <div className="stack">
      <article className="card">
        <h3>Détail du virement</h3>
        {fields.length ? (
          <dl className="kv">
            {fields.map((field) => (
              <span key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></span>
            ))}
          </dl>
        ) : (
          <p className="label">Aucun détail de virement sur ce dossier.</p>
        )}
      </article>
      {profile.length ? (
        <article className="card">
          <h3>Client</h3>
          <dl className="kv">
            {profile.map((field) => (
              <span key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></span>
            ))}
          </dl>
        </article>
      ) : null}
    </div>
  );
}

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
  const primaryLabel = (fundingReview ? ACTIVATE_TASK_ACTION[reviewState] : TASK_ACTION[reviewState])
    || (isAnalyst ? "Transmettre au contrôle" : "Approuver");
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
              <h3>{fundingReview && isChecker ? "Approuver le KYC" : isAnalyst ? "Transmettre" : isChecker ? "Approuver" : "Décision"}</h3>
              <div className="form">
                <textarea
                  value={approveNote}
                  onChange={(event) => onApproveNote(event.target.value)}
                  placeholder={fundingReview && isChecker ? "Note d'activation KYC" : isAnalyst ? "Note de transmission" : "Note d'approbation"}
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
          {canDecide && !fundingReview ? (
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
  const { t } = useI18n();
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

  function applyCaseColors(runtimeId: string, updated: Detail) {
    setQueue((current) =>
      current.map((item) =>
        item.id !== runtimeId
          ? item
          : {
              ...item,
              alert: Boolean(updated.alert),
              documents: updated.documents?.length ?? item.documents,
              revisions: updated.documents?.filter((doc) => doc.decision === "revisions").length ?? item.revisions,
              status: updated.case.status,
              state: updated.case.state,
            }
      )
    );
  }

  async function refreshSelectedCase(runtimeId = selected) {
    if (!runtimeId) return null;
    const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`);
    setDetail(updated);
    applyCaseColors(runtimeId, updated);
    return updated;
  }

  useEffect(() => {
    void loadQueue(undefined, query, page).catch((err: Error) => setError(err.message));
  }, [query, page]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    void api<Detail>(`/api/clarity/v1/desks/kyc/${selected}`)
      .then((updated) => {
        setDetail(updated);
        applyCaseColors(selected, updated);
      })
      .catch((err: Error) => setError(err.message));
  }, [selected]);

  async function decideCase(action: "approve" | "reject" | "revision" | "approve-partial", note = "") {
    setError("");
    try {
      const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/decision`, {
        method: "POST",
        body: JSON.stringify({ action, rationale: note }),
      });
      setDetail(updated);
      if (selected) applyCaseColors(selected, updated);
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
      if (selected) applyCaseColors(selected, refreshed);
      const found = (refreshed.identity || []).some((field) => ["firstName", "lastName", "dateOfBirth"].includes(field.key) && field.value);
      setMessage(found ? "Lecture enregistrée." : "Aucune identité lue sur la carte ou le passeport.");
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
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/identity`, {
        method: "PATCH",
        body: JSON.stringify({ fields: draft }),
      });
      setDetail(refreshed);
      if (selected) applyCaseColors(selected, refreshed);
      setMessage("Données d'identité enregistrées.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function disposition(id: string, status: "no_hit" | "confirmed_hit") {
    setError("");
    try {
      await api(`/api/clarity/v1/screening/matches/${id}/disposition`, { method: "POST", body: JSON.stringify({ status }) });
      await refreshSelectedCase();
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
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/document-requests`, {
        method: "POST",
        body: JSON.stringify({ reason: askReason, items: [item] }),
      });
      setDetail(refreshed);
      if (selected) applyCaseColors(selected, refreshed);
      setAskReason("");
      setResubmitId("");
      setComplementTarget("");
      setMessage(mode === "resubmit" ? "Renvoi demandé au client." : "Pièce complémentaire demandée au client.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demande refusée");
    }
  }

  async function compareFaces() {
    if (!selected) return;
    setError("");
    setComparing(true);
    try {
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/face-match`, { method: "POST" });
      setDetail(refreshed);
      applyCaseColors(selected, refreshed);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Comparaison des visages refusée");
    } finally {
      setComparing(false);
    }
  }

  async function decideDocument(id: string, decision: "approve" | "reject" | "revision") {
    setError("");
    try {
      await api<ViewerDocument>(`/api/clarity/v1/desks/documents/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await refreshSelectedCase();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision document refusée");
    }
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
  const screeningFactors = (detail?.customer?.risk?.factors || []).filter((factor) =>
    factor.code === "screening_confirmed" || factor.code === "screening_candidate"
  );
  const alertReasons = detail?.alert_reasons || [];
  const selectedIndex = queue.findIndex((item) => item.id === selected);
  const canPrevCase = selectedIndex > 0;
  const canNextCase = selectedIndex >= 0 && selectedIndex < queue.length - 1;

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
      <h1>{t("verify.title")}</h1>
      <p className="lede">{t("verify.lede")}</p>
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
      <p className="label">{total} {t("verify.cases")} · {t("verify.page")} {page + 1}</p>
      <div className="kyc-shell">
        <aside className="queue-panel" aria-label={t("verify.queue")}>
          <div className="panel-head">
            <h3>{t("verify.queue")}</h3>
            <span className="label">{queue.length}</span>
          </div>
          <div className="queue">
            {queue.map((item) => (
              <button className={`${item.id === selected ? "active" : "ghost"}${item.alert ? " alert" : ""}`} key={item.id} type="button" onClick={() => setSelected(item.id)}>
                <span>{item.name}</span>
                <span className="label">{item.status === "active" ? stepLabel(item.state, item.status) : statusLabel(item.status, item.state)} · {item.documents} pièce(s){item.revisions ? ` · ${item.revisions} à renvoyer` : ""}</span>
              </button>
            ))}
          </div>
          <div className="queue-nav">
            <button
              className="ghost"
              type="button"
              disabled={!canPrevCase && page === 0}
              onClick={() => {
                if (canPrevCase) {
                  setSelected(queue[selectedIndex - 1].id);
                  return;
                }
                if (page > 0) setPage((current) => current - 1);
              }}
            >
              {t("verify.prev")}
            </button>
            <button
              className="ghost"
              type="button"
              disabled={!canNextCase && (page + 1) * 25 >= total}
              onClick={() => {
                if (canNextCase) {
                  setSelected(queue[selectedIndex + 1].id);
                  return;
                }
                if ((page + 1) * 25 < total) setPage((current) => current + 1);
              }}
            >
              {t("verify.next")}
            </button>
          </div>
        </aside>
        {detail ? (
          <div className="case-panel case-panel-score" aria-label={t("verify.detail")}>
          <RiskScoreChip
            score={detail.customer?.risk?.score}
            tier={detail.customer?.risk?.tier}
            note={detail.customer?.risk?.formula_note}
            factors={detail.customer?.risk?.factors || []}
            labels={{
              score: t("verify.score"),
              breakdown: t("verify.score.breakdown"),
              empty: t("verify.score.empty"),
              click: t("verify.score.click"),
            }}
          />
          <div className="case-sheet">
            <header className="case-bar">
              <div>
                <p className="panel-kicker">{t("verify.detail")}</p>
                <h2>{detail.customer?.display_name || detail.case.name}</h2>
                <p className="label">{detail.case.definition} · {statusLabel(detail.case.status, detail.case.state)} · {detail.task?.name || stepLabel(detail.case.state, "sans tâche ouverte")}</p>
                {detail.steps?.length ? (
                  <ol className="stepper">
                    {detail.steps.map((step) => (
                      <li className={step.status} key={step.key}><span className="dot" />{step.label}</li>
                    ))}
                  </ol>
                ) : null}
              </div>
            </header>
            <div className="case-layout">
            <div className="tabs case-tabs">
              {TABS.map((item) => (
                <button
                  className={`${tab === item ? "active" : ""}${tabHasAlert(item, alertReasons) ? " tab-alert" : ""}`}
                  key={item}
                  type="button"
                  onClick={() => setTab(item)}
                >
                  {item}
                </button>
              ))}
            </div>
            <div className="case-pane">
            {tab === "Informations saisies" ? (
              fundingReview ? (
                <TransferDetail transfer={detail.transfer} submitted={detail.submitted || []} />
              ) : (
                <article className="card">
                  {detail.submitted?.length ? (
                    <dl className="kv">
                      {detail.submitted.map((field) => (
                        <span key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></span>
                      ))}
                    </dl>
                  ) : <p className="label">Aucune information saisie sur ce dossier.</p>}
                </article>
              )
            ) : null}
            {tab === "Documents" ? (
              <div className="document-data">
                <h3>Liste des pièces</h3>
                {!detail.documents.length ? (
                  <p className="label">
                    {fundingReview
                      ? "Pièces KYC du client (référence). L'activation se décide sur le virement."
                      : detail.case.state === "collect"
                      ? "Aucune pièce pour l'instant. Le dossier est encore en collecte — les documents apparaissent après l'envoi du parcours client."
                      : "Aucune pièce fournie."}
                  </p>
                ) : null}
                <DocumentViewer
                  documents={detail.documents}
                  locked={Boolean(finished) || fundingReview}
                  onDecide={finished || fundingReview ? undefined : (id, decision) => void decideDocument(id, decision)}
                />
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
                  <ScreeningMatchRow
                    key={match.id}
                    match={match}
                    pendingActions={match.status === "pending" ? {
                      onClear: () => void disposition(match.id, "no_hit"),
                      onConfirm: () => void disposition(match.id, "confirmed_hit"),
                    } : null}
                  />
                )) : (
                  <p className="label">
                    {detail.case.state === "collect"
                      ? "Criblage non lancé. Il démarre à la fin de la collecte client."
                      : "Aucune correspondance sur ce dossier."}
                  </p>
                )}
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
                {!canDecide && !finished ? (
                  <p className="label">
                    {detail.case.state === "collect"
                      ? "Aucune action de revue pour l'instant. Le dossier est encore en collecte — les décisions s'ouvrent à l'étape Revue analyste."
                      : `Aucune action ouverte à l'étape « ${stepLabel(detail.case.state, detail.task?.name || "en cours")} ».`}
                  </p>
                ) : null}
                {fundingReview && !finished ? <p className="label">Virement reçu sur un compte limité. Vérifiez le détail du transfert puis approuvez le KYC pour activer le compte.</p> : null}
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

  async function refreshPreview() {
    const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`);
    setDetail(updated);
    onChanged?.();
    return updated;
  }

  async function compareFaces() {
    setError("");
    setComparing(true);
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/face-match`, { method: "POST" }));
      onChanged?.();
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

  async function decideDocument(id: string, decision: "approve" | "reject" | "revision") {
    setError("");
    try {
      await api<ViewerDocument>(`/api/clarity/v1/desks/documents/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await refreshPreview();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision document refusée");
    }
  }

  async function disposition(id: string, status: "no_hit" | "confirmed_hit") {
    setError("");
    try {
      await api(`/api/clarity/v1/screening/matches/${id}/disposition`, { method: "POST", body: JSON.stringify({ status }) });
      await refreshPreview();
      setMessage(status === "no_hit" ? "Correspondance écartée." : "Correspondance confirmée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vérification du criblage refusée");
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
  const docsLocked = finished || !canDecide;
  const sentDocuments = (detail.documents || []).filter((document) => document.status !== "requested");
  const complements = (detail.tracker || []).filter((row) => !row.document_id);
  const alertReasons = detail.alert_reasons || [];
  return (
    <div className="verify-preview">
      <aside className="verify-side">
        <div className="verify-side-head">
          <h3>{detail.customer?.display_name || detail.case.name}</h3>
          <RiskScoreChip
            score={detail.customer?.risk?.score}
            tier={detail.customer?.risk?.tier}
            note={detail.customer?.risk?.formula_note}
            factors={detail.customer?.risk?.factors || []}
          />
        </div>
        <div className="tabs case-tabs">
          {PREVIEW_TABS.map((item) => (
            <button
              className={`${tab === item ? "active" : ""}${tabHasAlert(item, alertReasons) ? " tab-alert" : ""}`}
              key={item}
              type="button"
              onClick={() => setTab(item)}
            >
              {item}
            </button>
          ))}
        </div>
      </aside>
      <div>
        {error ? <p className="error">{error}</p> : null}
        {message ? <p className="label">{message}</p> : null}
        {tab === "Informations saisies" ? (
          fundingReview ? (
            <TransferDetail transfer={detail.transfer} submitted={detail.submitted || []} />
          ) : (
            <article className="card">
              {detail.submitted?.length ? (
                <dl className="kv">
                  {detail.submitted.map((field) => (
                    <span key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></span>
                  ))}
                </dl>
              ) : <p className="label">Aucune information saisie sur ce dossier.</p>}
            </article>
          )
        ) : null}
        {tab === "Documents" ? (
          <DocumentViewer
            documents={detail.documents}
            locked={docsLocked || fundingReview}
            onDecide={docsLocked || fundingReview ? undefined : (id, decision) => void decideDocument(id, decision)}
          />
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
        {tab === "Criblage" ? (
          <article className="card">
            {(detail.customer?.risk?.factors || [])
              .filter((factor) => factor.code === "screening_confirmed" || factor.code === "screening_candidate")
              .map((factor) => (
                <p className="factor" key={factor.code}>{factor.points} pts — {factor.explanation}</p>
              ))}
            {matches.length ? matches.map((match) => (
              <ScreeningMatchRow
                key={match.id}
                match={match}
                pendingActions={
                  !finished && match.status === "pending"
                    ? {
                        onClear: () => void disposition(match.id, "no_hit"),
                        onConfirm: () => void disposition(match.id, "confirmed_hit"),
                      }
                    : null
                }
              />
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
            {fundingReview && !finished ? <p className="label">Virement reçu sur un compte limité. Vérifiez le détail du transfert puis approuvez le KYC pour activer le compte.</p> : null}
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
