"use client";

import { useEffect, useState } from "react";
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
    screening: Array<{ id: string; status: string; production: boolean; matches: Array<{ id: string; status: string; score: number }> }>;
    verification_checks: Array<Record<string, unknown>>;
  } | null;
  documents: ViewerDocument[];
  tracker: Array<{ id?: string; type: string; category: string; country?: string; required: boolean; status: string; document_id?: string | null }>;
  notice: string;
};

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

  const person = detail?.customer?.individual;
  const taskKey = detail?.task?.key || "";
  const revisions = detail?.documents.filter((item) => item.decision === "revisions").length || 0;

  return (
    <>
      <h1>Vérification</h1>
      <p className="lede">Revue du dossier et des pièces, comme sur un bureau de conformité. Approuver une pièce ne confirme pas l'identité.</p>
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
              <div className="tabs">
                <button className="ghost" type="button" disabled={!revisions} onClick={() => void decideCase("revision")}>Demander les renvois{revisions ? ` (${revisions})` : ""}</button>
                <button className="ghost" type="button" onClick={() => void decideCase("reject")}>Rejeter</button>
                <button type="button" onClick={() => void decideCase("approve")}>{TASK_ACTION[taskKey] || "Approuver"}</button>
              </div>
            </header>
            <div className="kyc-body">
              <aside className="info-pane">
                <h3>Informations personnelles</h3>
                <dl className="kv">
                  <dt>Nom</dt><dd>{person?.legal_name || detail.customer?.display_name || "—"}</dd>
                  <dt>Naissance</dt><dd>{person?.date_of_birth || "—"}</dd>
                  <dt>Nationalité</dt><dd>{person?.nationality || "—"}</dd>
                  <dt>Adresse</dt><dd>{person?.address || "—"}</dd>
                  <dt>E-mail</dt><dd>{person?.email || "—"}</dd>
                  <dt>Statut</dt><dd>{detail.customer?.status || "—"}</dd>
                </dl>
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
                <h3>Contrôles</h3>
                <p className="label">{detail.notice}</p>
                {(detail.customer?.verification_checks || []).map((check) => (
                  <p className="factor" key={String(check.id)}>{String(check.kind)} · {String(check.status)} · production {String(check.production)}</p>
                ))}
                {(detail.customer?.screening || []).flatMap((item) => item.matches || []).slice(0, 4).map((match) => (
                  <p className="factor" key={match.id}>{match.status} · score {match.score} · une correspondance n'est pas une confirmation</p>
                ))}
              </aside>
              <DocumentViewer documents={detail.documents} onDecide={(id, decision) => void decideDocument(id, decision)} />
            </div>
          </div>
        ) : <p>Chargement du dossier…</p>}
      </div>
    </>
  );
}
