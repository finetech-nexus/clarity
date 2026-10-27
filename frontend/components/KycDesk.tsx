"use client";

import { FormEvent, useEffect, useState } from "react";
import DocumentViewer, { ViewerDocument } from "./DocumentViewer";
import { api, searchPage } from "../lib/api";

type QueueItem = {
  id: string;
  name: string;
  definition: string;
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
  tracker: Array<{ id?: string; type: string; category: string; country?: string; required: boolean; status: string; document_id?: string | null }>;
  steps?: Array<{ key: string; label: string; status: "done" | "current" | "upcoming" }>;
};

const TABS = ["Informations saisies", "Documents", "Données du document", "Résultats", "Criblage"] as const;
const PREVIEW_TABS = ["Informations saisies", "Documents", "Données du document", "Résultats", "Score", "Criblage"] as const;

const STEP_LABEL: Record<string, string> = {
  collect: "Collecte",
  verify: "Vérification",
  screen: "Criblage",
  score: "Évaluation du risque",
  analystReview: "Revue analyste",
  checkerReview: "Contrôle checker",
  remediate: "Complément",
  investigate: "Investigation",
  approved: "Approuvé",
  rejected: "Rejeté",
  closed: "Clôturé",
};

function stepLabel(state: string | null | undefined, fallback: string) {
  if (!state) return fallback;
  return STEP_LABEL[state] || state;
}

const STATUS_FILTERS = [
  { value: "active", label: "En cours" },
  { value: "completed", label: "Terminée" },
  { value: "failed", label: "Échec" },
];

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

export default function KycDesk() {
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

  async function loadQueue(prefer?: string, nextQuery = query, nextPage = page) {
    const body = await searchPage<QueueItem>("/api/clarity/v1/desks/kyc", nextQuery, nextPage);
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

  async function decideCase(action: "approve" | "reject" | "revision") {
    setError("");
    try {
      const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/decision`, {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      setDetail(updated);
      setMessage(action === "revision" ? "Renvoi demandé. Le client doit fournir les pièces marquées." : "Décision enregistrée sur le dossier.");
      await loadQueue(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision refusée");
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
  const revisions = detail?.documents.filter((item) => item.decision === "revisions").length || 0;
  const matches = (detail?.customer?.screening || []).flatMap((item) => item.matches || []);
  const piecesApproved = Boolean(detail?.documents.length) && detail!.documents.every((item) => item.decision === "approved");
  const screeningPending = matches.some((item) => item.status === "pending");
  const canApprove = Boolean(taskKey) && piecesApproved && !screeningPending;

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
              <span className="label">{stepLabel(item.state, item.status)} · {item.documents} pièce(s){item.revisions ? ` · ${item.revisions} à renvoyer` : ""}</span>
            </button>
          ))}
        </div>
        {detail ? (
          <div className={`case-sheet${detail.alert ? " alert" : ""}`}>
            <header className="case-bar">
              <div>
                <h2>{detail.customer?.display_name || detail.case.name}</h2>
                <p className="label">{detail.case.definition} · {detail.case.status} · {detail.task?.name || stepLabel(detail.case.state, "sans tâche ouverte")}</p>
                {detail.steps?.length ? (
                  <ol className="stepper">
                    {detail.steps.map((step) => (
                      <li className={step.status} key={step.key}><span className="dot" />{step.label}</li>
                    ))}
                  </ol>
                ) : null}
                {detail.alert_reasons?.includes("correspondence") ? <p className="alert-line">Les données lues sur la pièce ne correspondent pas aux informations saisies.</p> : null}
                {detail.alert_reasons?.includes("screening") ? <p className="alert-line">Le criblage remonte une alerte.</p> : null}
                {detail.customer?.risk?.factors?.length ? (
                  <ul className="factors">
                    {detail.customer.risk.factors.map((factor) => (
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
              <div className="actions">
                <button className="ghost" type="button" disabled={!revisions || !taskKey} onClick={() => void decideCase("revision")}>Demander les renvois{revisions ? ` (${revisions})` : ""}</button>
                <button className="ghost" type="button" disabled={!taskKey} onClick={() => void decideCase("reject")}>Rejeter</button>
                <button type="button" disabled={!canApprove} onClick={() => void decideCase("approve")}>{TASK_ACTION[taskKey] || "Approuver"}</button>
                {!piecesApproved ? <p className="label">Approuvez chaque pièce pour activer la validation.</p> : null}
                {screeningPending ? <p className="label">Vérifiez chaque correspondance de criblage avant de valider.</p> : null}
              </div>
            </header>
            <div className="tabs">
              {TABS.map((item) => (
                <button className={tab === item ? "active" : ""} key={item} type="button" onClick={() => setTab(item)}>{item}</button>
              ))}
            </div>
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
            {tab === "Résultats" ? (
              <article className="card">
                {detail.documents.map((item) => (
                  <p key={item.id}><strong>{item.title}</strong> · {item.decision || item.status || "en attente"}</p>
                ))}
                {(detail.customer?.verification_checks || []).filter((check) => check.status !== "not_a_production_verification").map((check) => (
                  <p className="factor" key={String(check.id)}>{String(check.kind)} · {String(check.status)}</p>
                ))}
                {!detail.documents.length && !(detail.customer?.verification_checks || []).length ? <p className="label">Aucun résultat sur ce dossier.</p> : null}
              </article>
            ) : null}
            {tab === "Criblage" ? (
              <article className="card">
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
          </div>
        ) : selected ? <p>Chargement du dossier…</p> : null}
      </div>
    </>
  );
}

export function VerificationPreview({ runtimeId }: { runtimeId: string }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<(typeof PREVIEW_TABS)[number]>("Informations saisies");

  useEffect(() => {
    setDetail(null);
    setError("");
    setTab("Informations saisies");
    void api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`).then(setDetail).catch((err: Error) => setError(err.message));
  }, [runtimeId]);

  if (error) return <p className="error">{error}</p>;
  if (!detail) return <p className="label">Chargement de la vérification…</p>;
  const matches = (detail.customer?.screening || []).flatMap((item) => item.matches || []);
  return (
    <div className="verify-preview">
      <aside className="verify-side">
        <h3>{detail.customer?.display_name || detail.case.name}</h3>
        <div className="tabs">
          {PREVIEW_TABS.map((item) => (
            <button className={tab === item ? "active" : ""} key={item} type="button" onClick={() => setTab(item)}>{item}</button>
          ))}
        </div>
      </aside>
      <div>
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
        {tab === "Résultats" ? (
          <article className="card">
            {detail.documents.map((item) => (
              <p key={item.id}><strong>{item.title}</strong> · {item.decision || item.status || "en attente"}</p>
            ))}
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
      </div>
    </div>
  );
}
