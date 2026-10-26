"use client";

import { FormEvent, useEffect, useState } from "react";
import DocumentViewer, { ViewerDocument } from "./DocumentViewer";
import { QueryStack } from "./QueryStack";
import { api, searchPage } from "../lib/api";

type CaseRow = {
  id: string;
  name: string;
  status: string;
  outcome: string;
  type?: string;
  assignee?: { id: string; name: string } | null;
  tags?: Array<{ id: string; name: string }>;
  created_at?: string;
  due_at?: string | null;
  snoozed_until?: string | null;
  escalated?: boolean;
};

type Manager = {
  case: CaseRow;
  inbox: { id: string; name: string } | null;
  customer: {
    id: string;
    display_name: string;
    kind: string;
    status: string;
    risk_tier: string | null;
    individual: Record<string, string | null> | null;
    business: Record<string, string | null> | null;
  } | null;
  documents: ViewerDocument[];
  files: Array<{ id: string; fileName: string; url: string }>;
  comments: Array<{ id: string; comment: string; created_at: string }>;
  events: Array<{ id: string; action: string; actor: string; created_at: string }>;
  alerts: Array<{ id: string; title: string; status: string; priority: string; rationale: string | null }>;
  decisions: Array<{ id: string; outcome: string; score: number; created_at: string }>;
  transactions: Array<{ id: string; amount: number; currency: string; country: string; description: string }>;
  screening: Array<{ id: string; name: string; status: string; score: number }>;
  reports: Array<{ id: string; type: string; status: string; submission_channel: string }>;
  users: Array<{ id: string; name: string }>;
};

const TABS = ["Principal", "AML", "Clients concernés", "Documents"] as const;
const OUTCOMES = [
  ["false_positive", "Faux positif"],
  ["valuable_alert", "Alerte utile"],
  ["confirmed_risk", "Risque confirmé"],
  ["unset", "Non qualifié"],
];

export default function CaseDesk() {
  const [rows, setRows] = useState<CaseRow[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Manager | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Principal");
  const [comment, setComment] = useState("");
  const [outcome, setOutcome] = useState("false_positive");
  const [narrative, setNarrative] = useState("");
  const [review, setReview] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);

  async function loadList(nextQuery = query, nextPage = page) {
    const body = await searchPage<CaseRow>("/api/clarity/v1/desks/cases", nextQuery, nextPage);
    setRows(body.items);
    setTotal(body.total);
  }

  async function openCase(id: string) {
    setSelected(id);
    setTab("Principal");
    setReview("");
    setDetail(await api<Manager>(`/api/clarity/v1/desks/cases/${id}`));
  }

  useEffect(() => {
    void loadList(query, page).catch((err: Error) => setError(err.message));
  }, [query, page]);

  async function refresh() {
    if (!selected) return;
    setDetail(await api<Manager>(`/api/clarity/v1/desks/cases/${selected}`));
    await loadList();
  }

  async function act(path: string, body?: unknown) {
    setError("");
    try {
      await api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action refusée");
    }
  }

  async function sendComment(event: FormEvent) {
    event.preventDefault();
    await act(`/api/clarity/v1/desks/cases/${selected}/comment`, { comment });
    setComment("");
  }

  async function askReview() {
    const created = await api<{ text?: string; provider?: string; production?: boolean }>(`/cases/${selected}/review/enqueue`, { method: "POST", body: "{}" });
    setReview(`${created.provider || "analyste"} · production ${String(created.production)} — ${created.text || ""}`);
  }

  async function prepareSar(event: FormEvent) {
    event.preventDefault();
    const report = await api<{ submission_channel: string; notice: string }>(`/api/clarity/v1/cases/${selected}/reports`, {
      method: "POST",
      body: JSON.stringify({ report_type: "suspicious_activity", narrative }),
    });
    setNarrative("");
    setReview(`${report.notice} Canal : ${report.submission_channel}.`);
    await refresh();
  }

  const next = rows.find((row) => row.id !== selected && !row.assignee && row.status !== "closed");

  return (
    <>
      <h1>Investigations</h1>
      <p className="lede">File des dossiers, puis fiche : identité, alertes, pièces et qualification. Le texte d'aide ne qualifie pas un blanchiment.</p>
      {error ? <p className="error">{error}</p> : null}
      {!selected ? (
        <>
        <QueryStack
          query={query}
          onChange={(next) => { setPage(0); setQuery(next); }}
          fields={[{ key: "status", label: "Statut", options: ["pending", "investigating", "closed"] }, { key: "outcome", label: "Revue" }, { key: "assignee", label: "Assigné" }, { key: "name", label: "Nom" }]}
          total={total}
          page={page}
          pageSize={25}
          onPage={setPage}
        />
        <table className="inbox">
          <thead>
            <tr>
              <th>Statut</th>
              <th>Nom de l'investigation</th>
              <th>Type</th>
              <th>Revue</th>
              <th>Assigné</th>
              <th>Tags</th>
              <th>Date</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} onClick={() => void openCase(row.id)}>
                <td><span className={`badge ${row.status}`}>{row.status}</span></td>
                <td>{row.name}</td>
                <td>{row.type || "decision"}</td>
                <td>{row.outcome}</td>
                <td>{row.assignee?.name || "Non assigné"}</td>
                <td>{(row.tags || []).map((tag) => tag.name).join(", ") || "—"}</td>
                <td>{row.created_at ? new Date(row.created_at).toLocaleDateString("fr-FR") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </>
      ) : detail ? (
        <article className="case-sheet">
          <header className="case-bar">
            <div>
              <button className="ghost" type="button" onClick={() => { setSelected(""); setDetail(null); }}>Retour à la file</button>
              <h2>{detail.case.name}</h2>
              <p className="label">{detail.inbox?.name} · {detail.case.status} · {detail.case.outcome}{detail.case.snoozed_until ? ` · reporté jusqu'au ${new Date(detail.case.snoozed_until).toLocaleDateString("fr-FR")}` : ""}</p>
            </div>
            <div className="tabs">
              {detail.case.status !== "closed" ? (
                <>
                  <button className="ghost" type="button" onClick={() => void act(`/api/clarity/v1/desks/cases/${selected}/snooze`, { days: 2 })}>Reporter</button>
                  <button className="ghost" type="button" onClick={() => void act(`/api/clarity/v1/cases/${selected}/escalate`)}>Escalader</button>
                  <button type="button" onClick={() => void act(`/api/clarity/v1/desks/cases/${selected}/close`, { outcome })}>Clôturer</button>
                </>
              ) : (
                <button type="button" onClick={() => void act(`/api/clarity/v1/desks/cases/${selected}/reopen`)}>Rouvrir</button>
              )}
              {next ? <button className="ghost" type="button" onClick={() => void openCase(next.id)}>Dossier suivant</button> : null}
            </div>
          </header>
          <div className="tabs">
            {TABS.map((item) => (
              <button className={tab === item ? "active" : ""} key={item} type="button" onClick={() => setTab(item)}>{item}</button>
            ))}
          </div>
          {tab === "Principal" ? (
            <div className="manager-grid">
              <section className="card">
                <p><span className={`badge ${detail.case.status}`}>{detail.case.status}</span> {(detail.case.tags || []).map((tag) => <span className="tag" key={tag.id}>{tag.name}</span>)}</p>
                <dl className="kv">
                  <dt>Identifiant</dt><dd>{detail.case.id}</dd>
                  <dt>Assigné à</dt>
                  <dd>
                    <select aria-label="Assigné" value={detail.case.assignee?.id || ""} onChange={(event) => void act(`/api/clarity/v1/desks/cases/${selected}/assign`, { user_id: event.target.value })}>
                      <option value="">Non assigné</option>
                      {detail.users.map((user) => <option key={user.id} value={user.id}>{user.name}</option>)}
                    </select>
                  </dd>
                  <dt>File</dt><dd>{detail.inbox?.name || "—"}</dd>
                  <dt>Issue</dt>
                  <dd>
                    <select aria-label="Issue" value={outcome} onChange={(event) => setOutcome(event.target.value)}>
                      {OUTCOMES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </dd>
                </dl>
                <h3>Événements</h3>
                <div className="timeline">
                  {detail.events.length ? detail.events.map((event) => (
                    <p key={event.id}><strong>{event.action}</strong> · {event.actor}<span className="label"> {new Date(event.created_at).toLocaleString("fr-FR")}</span></p>
                  )) : <p className="label">Aucun événement enregistré.</p>}
                  {detail.comments.map((note) => <p key={note.id}><strong>Note</strong> · {note.comment}</p>)}
                </div>
              </section>
              <section>
                <article className="card">
                  <h3>Revue assistée</h3>
                  <button type="button" onClick={() => void askReview()}>Demander une revue</button>
                  {review ? <p>{review}</p> : <p className="label">La revue prépare l'instruction. Elle ne confirme ni une correspondance ni un blanchiment.</p>}
                </article>
                <article className="card">
                  <h3>Alertes</h3>
                  {detail.alerts.length ? detail.alerts.map((alert) => (
                    <p key={alert.id}><strong>{alert.title}</strong><span className="label"> {alert.priority} · {alert.status}<br />{alert.rationale}</span></p>
                  )) : <p className="label">Aucune alerte liée.</p>}
                  {detail.transactions.map((tx) => <p className="factor" key={tx.id}>{tx.amount} {tx.currency} · {tx.country} · {tx.description}</p>)}
                </article>
                <article className="card">
                  <h3>Documents</h3>
                  <div className="file-row">
                    {detail.files.map((file) => <span className="tag" key={file.id}>{file.fileName}</span>)}
                    {!detail.files.length ? <span className="label">Aucune pièce versée sur le dossier.</span> : null}
                  </div>
                  <form className="form" onSubmit={sendComment}>
                    <textarea className="wide" value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Note d'investigation" aria-label="Note d'investigation" />
                    <button type="submit">Ajouter la note</button>
                  </form>
                  <form className="form" onSubmit={prepareSar}>
                    <textarea className="wide" value={narrative} onChange={(event) => setNarrative(event.target.value)} placeholder="Faits pour une déclaration, sans dépôt" aria-label="Déclaration" />
                    <button className="ghost" type="submit">Signaler une activité suspecte</button>
                  </form>
                </article>
              </section>
            </div>
          ) : null}
          {tab === "AML" ? (
            <div className="manager-grid">
              <section className="card">
                <h3>Décisions</h3>
                {detail.decisions.length ? detail.decisions.map((row) => <p key={row.id}>{row.outcome} · score {row.score}</p>) : <p className="label">Aucune décision rattachée.</p>}
              </section>
              <section className="card">
                <h3>Criblage</h3>
                {detail.screening.length ? detail.screening.map((match) => (
                  <p key={match.id}>{String(match.name)} · {match.status} · {match.score}<span className="label"> Correspondance candidate, non confirmée.</span></p>
                )) : <p className="label">Pas de correspondance sur le client du dossier.</p>}
                <h3>Déclarations</h3>
                {detail.reports.map((report) => <p key={report.id}>{report.type} · {report.status} · {report.submission_channel}</p>)}
              </section>
            </div>
          ) : null}
          {tab === "Clients concernés" ? (
            <article className="card">
              {detail.customer ? (
                <dl className="kv">
                  <dt>Client</dt><dd>{detail.customer.display_name}</dd>
                  <dt>Type</dt><dd>{detail.customer.kind}</dd>
                  <dt>Statut</dt><dd>{detail.customer.status}</dd>
                  <dt>Risque</dt><dd>{detail.customer.risk_tier || "non évalué"}</dd>
                  <dt>Nom légal</dt><dd>{detail.customer.individual?.legal_name || detail.customer.business?.legal_name || "—"}</dd>
                  <dt>Adresse</dt><dd>{detail.customer.individual?.address || detail.customer.business?.registered_address || "—"}</dd>
                </dl>
              ) : <p className="label">Ce dossier n'est pas encore relié à un client.</p>}
            </article>
          ) : null}
          {tab === "Documents" ? (
            <div className="kyc-body">
              <aside className="info-pane">
                <h3>Pièces du dossier</h3>
                {detail.files.map((file) => <p key={file.id}>{file.fileName}</p>)}
              </aside>
              <DocumentViewer
                documents={[
                  ...detail.documents,
                  ...detail.files.map((file) => ({ id: file.id, title: file.fileName, fileName: file.fileName, url: file.url })),
                ]}
                onDecide={(id, decision) => {
                  if (!detail.documents.some((item) => item.id === id)) return;
                  void api(`/api/clarity/v1/desks/documents/${id}/decision`, { method: "POST", body: JSON.stringify({ decision }) }).then(() => refresh());
                }}
              />
            </div>
          ) : null}
        </article>
      ) : <p>Chargement du dossier…</p>}
    </>
  );
}
