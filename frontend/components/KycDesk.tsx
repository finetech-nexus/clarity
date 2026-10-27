"use client";

import { FormEvent, useEffect, useState } from "react";
import DocumentViewer, { ViewerDocument } from "./DocumentViewer";
import { QueryStack } from "./QueryStack";
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
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Informations saisies");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reading, setReading] = useState(false);

  async function loadQueue(prefer?: string, nextQuery = query, nextPage = page) {
    const body = await searchPage<QueueItem>("/api/clarity/v1/desks/kyc", nextQuery, nextPage);
    setQueue(body.items);
    setTotal(body.total);
    const next = prefer && body.items.some((row) => row.id === prefer) ? prefer : body.items[0]?.id || "";
    setSelected(next);
  }

  useEffect(() => {
    void loadQueue(undefined, query, page).catch((err: Error) => setError(err.message));
  }, [query, page]);

  useEffect(() => {
    if (!selected) return;
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
      const paddle = (refreshed.documents || []).some((item) => item.ocr?.engine === "paddleocr");
      setMessage(found
        ? paddle
          ? "Lecture par le modèle PP-OCRv5. Ce n'est pas une vérification de production."
          : "Lecture enregistrée à partir de la carte d'identité et du passeport. Ce n'est pas une vérification de production."
        : "Aucune identité lue sur la carte ou le passeport.");
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

  return (
    <>
      <h1>Vérification</h1>
      <p className="lede">Dossier de vérification : informations saisies, pièces et données lues sur les documents.</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <QueryStack
        query={query}
        onChange={(next) => { setPage(0); setQuery(next); }}
        fields={[{ key: "status", label: "Statut", options: ["active", "completed", "failed"] }, { key: "state", label: "Étape" }, { key: "name", label: "Nom" }]}
        total={total}
        page={page}
        pageSize={25}
        onPage={setPage}
      />
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
              </div>
              <div className="actions">
                <button className="ghost" type="button" disabled={!revisions || !taskKey} onClick={() => void decideCase("revision")}>Demander les renvois{revisions ? ` (${revisions})` : ""}</button>
                <button className="ghost" type="button" disabled={!taskKey} onClick={() => void decideCase("reject")}>Rejeter</button>
                <button type="button" disabled={!taskKey} onClick={() => void decideCase("approve")}>{TASK_ACTION[taskKey] || "Approuver"}</button>
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
              <div className="kyc-body">
                <aside className="info-pane">
                  <h3>Liste des pièces</h3>
                  <div className="tracker">
                    {(detail.tracker || []).map((item) => (
                      <p key={`${item.type}-${item.category}-${item.document_id || "missing"}`}>
                        <span className={`badge ${item.status}`}>{item.status}</span> {item.type}
                        <span className="label"> {item.category} · {item.country || "ZZ"} · {item.required ? "obligatoire" : "facultatif"}</span>
                      </p>
                    ))}
                    {!detail.tracker?.length ? <p className="label">Aucun type configuré sur ce parcours.</p> : null}
                  </div>
                </aside>
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
                    <p className="label">Une seule lecture PP-OCRv5 pour la carte d'identité et le passeport. Ce n'est pas une vérification de production.</p>
                  </form>
                ) : <p className="label">Aucune carte d'identité ou passeport sur ce dossier.</p>}
              </article>
            ) : null}
            {tab === "Résultats" ? (
              <article className="card">
                {detail.documents.map((item) => (
                  <p key={item.id}><strong>{item.title}</strong> · {item.decision || item.status || "en attente"}</p>
                ))}
                {(detail.customer?.verification_checks || []).map((check) => (
                  <p className="factor" key={String(check.id)}>{String(check.kind)} · {String(check.status)}</p>
                ))}
                {!detail.documents.length && !(detail.customer?.verification_checks || []).length ? <p className="label">Aucun résultat sur ce dossier.</p> : null}
              </article>
            ) : null}
            {tab === "Criblage" ? (
              <article className="card">
                {matches.length ? matches.map((match) => (
                  <p key={match.id}>{match.name || match.status} · {match.status} · score {match.score}</p>
                )) : <p className="label">Aucune correspondance sur ce dossier.</p>}
              </article>
            ) : null}
          </div>
        ) : <p>Chargement du dossier…</p>}
      </div>
    </>
  );
}
