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
};

type Field = { key: string; label: string; value: string };

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
  documents: Array<ViewerDocument & { extracted?: Field[]; decision?: string | null; status?: string; type?: string; category?: string }>;
  tracker: Array<{ id?: string; type: string; category: string; country?: string; required: boolean; status: string; document_id?: string | null }>;
};

const TABS = ["Informations saisies", "Documents", "Données du document", "Résultats", "Criblage"] as const;

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
  const [documentId, setDocumentId] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});

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

  const activeDocument = detail?.documents.find((item) => item.id === documentId) || detail?.documents[0];

  useEffect(() => {
    if (!activeDocument) return;
    const next: Record<string, string> = {};
    for (const field of activeDocument.extracted || []) next[field.key] = field.value;
    setDraft(next);
    if (activeDocument.id !== documentId) setDocumentId(activeDocument.id);
  }, [activeDocument?.id, selected]);

  async function saveExtracted(event: FormEvent) {
    event.preventDefault();
    if (!activeDocument) return;
    setError("");
    try {
      const updated = await api<ViewerDocument & { extracted?: Field[] }>(`/api/clarity/v1/desks/kyc/${selected}/documents/${activeDocument.id}`, {
        method: "PATCH",
        body: JSON.stringify({ fields: draft }),
      });
      setDetail((current) => current && ({
        ...current,
        documents: current.documents.map((item) => (item.id === updated.id ? { ...item, ...updated } : item)),
      }));
      setMessage("Données du document enregistrées.");
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
            <button className={item.id === selected ? "active" : "ghost"} key={item.id} type="button" onClick={() => setSelected(item.id)}>
              <span>{item.name}</span>
              <span className="label">{item.state === "analystReview" ? "manual review" : (item.state || item.status)} · {item.documents} pièce(s){item.revisions ? ` · ${item.revisions} à renvoyer` : ""}</span>
            </button>
          ))}
        </div>
        {detail ? (
          <div className="case-sheet">
            <header className="case-bar">
              <div>
                <h2>{detail.customer?.display_name || detail.case.name}</h2>
                <p className="label">{detail.case.definition} · {detail.case.status} · {detail.task?.name || detail.case.state || "sans tâche ouverte"}</p>
              </div>
              <div className="actions">
                <button className="ghost" type="button" disabled={!revisions} onClick={() => void decideCase("revision")}>Demander les renvois{revisions ? ` (${revisions})` : ""}</button>
                <button className="ghost" type="button" onClick={() => void decideCase("reject")}>Rejeter</button>
                <button type="button" onClick={() => void decideCase("approve")}>{TASK_ACTION[taskKey] || "Approuver"}</button>
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
                <div className="tabs">
                  {detail.documents.map((item) => (
                    <button className={item.id === activeDocument?.id ? "active" : ""} key={item.id} type="button" onClick={() => setDocumentId(item.id)}>{item.title}</button>
                  ))}
                </div>
                {activeDocument ? (
                  <form className="form" onSubmit={(event) => void saveExtracted(event)}>
                    {(activeDocument.extracted || []).map((field) => (
                      <label key={field.key}>
                        {field.label}
                        <input value={draft[field.key] ?? field.value} onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))} />
                      </label>
                    ))}
                    <button className="wide" type="submit">Enregistrer</button>
                  </form>
                ) : <p className="label">Aucune pièce sur ce dossier.</p>}
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
