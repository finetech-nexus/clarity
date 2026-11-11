"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";

type AlertRow = { id: string; title: string; status: string; customer_id?: string | null; payload?: { stance?: string; category?: string; identity_score?: number; media_score?: number; excerpt?: string; article_title?: string } };
type Summary = { sources: number; active_sources: number; open_alerts: number };
type SearchHit = { entity: string; identity_score: number; article_title: string; article_url?: string; published_at?: string; stance?: string; category?: string; explanation: string[] };
type SearchReport = { status: string; screening_status?: string; screening_meaning?: string; suggested_action?: string; results: SearchHit[]; identity_confirmed_by_name_alone?: boolean; retrieved?: number; period_start?: string; period_end?: string; years?: number };
type Batch = { status: string; progress: number; done: number; total: number; message: string };

export function AdverseMediaDesk() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [batch, setBatch] = useState<Batch>({ status: "idle", progress: 0, done: 0, total: 0, message: "" });
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [report, setReport] = useState<SearchReport | null>(null);
  const [rationale, setRationale] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const [nextSummary, nextAlerts, nextBatch] = await Promise.all([
      api<Summary>("/api/clarity/v1/adverse-media/summary"),
      api<AlertRow[]>("/api/clarity/v1/adverse-media/alerts"),
      api<Batch>("/api/clarity/v1/adverse-media/batch"),
    ]);
    setSummary(nextSummary);
    setAlerts(nextAlerts);
    setBatch(nextBatch);
  }

  useEffect(() => {
    void load().catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    if (batch.status !== "running") return;
    const timer = window.setInterval(() => {
      void api<Batch>("/api/clarity/v1/adverse-media/batch").then(setBatch).catch((err: Error) => setError(err.message));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [batch.status]);

  async function run(action: () => Promise<unknown>, done: string) {
    setError("");
    setMessage("");
    try {
      await action();
      await load();
      setMessage(done);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action refusée");
    }
  }

  return (
    <>
      <h1>Adverse media</h1>
      <p className="lede">Recherche de couverture négative. Le moteur ne conclut pas à la culpabilité. Les sources se règlent dans Administration, Third party.</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <div className="grid">
        <article className="card"><p className="label">Sources actives</p><strong>{summary?.active_sources ?? "—"} / {summary?.sources ?? "—"}</strong></article>
        <article className="card"><p className="label">Batch</p><strong>{batch.progress}%</strong></article>
        <article className="card"><p className="label">Alertes ouvertes</p><strong>{summary?.open_alerts ?? "—"}</strong></article>
      </div>
      <div style={{ height: 8, background: "#e6e6e6", marginBottom: 16 }}>
        <div style={{ width: `${Math.max(0, Math.min(batch.progress, 100))}%`, height: 8, background: "#1f6b4a" }} />
      </div>

      <article className="card">
        <h2>Recherche</h2>
        <form className="form" onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const body = await api<SearchReport>("/api/clarity/v1/adverse-media/search", { method: "POST", body: JSON.stringify({ name: query }) });
            setReport(body);
            setHits(body.results || []);
          }, "Recherche terminée.");
        }}>
          <label>Nom<input value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <button type="submit">Rechercher</button>
        </form>
        <p className="label">La recherche lit les articles déjà collectés. Le batch, sous Administration → Third party, lit cinq ans de Google Actualités et interroge GDELT depuis 2010 pour chaque nom déjà recherché.</p>
        {report ? <p className="label">{report.screening_status} — {report.screening_meaning} {report.suggested_action}</p> : null}
        {hits.map((hit) => <p key={`${hit.article_url || hit.article_title}`}>{hit.published_at || "date inconnue"} · {hit.article_title} · {hit.stance || ""} · {hit.category || ""}</p>)}
      </article>

      <h2>Résultats à revoir</h2>
      <label>Justification<textarea value={rationale} onChange={(event) => setRationale(event.target.value)} /></label>
      {alerts.length ? alerts.map((alert) => (
        <article className="card" key={alert.id}>
          <p>{alert.title} · {alert.status}</p>
          <p className="label">Identité {alert.payload?.identity_score ?? "—"} · média {alert.payload?.media_score ?? "—"} · {alert.payload?.stance} · {alert.payload?.category}</p>
          {alert.payload?.excerpt ? <p>{alert.payload.excerpt}</p> : null}
          <div className="actions">
            <button type="button" onClick={() => void run(() => api(`/api/clarity/v1/adverse-media/alerts/${alert.id}/decision`, { method: "POST", body: JSON.stringify({ outcome: "confirmed_relevant", rationale }) }), "Décision enregistrée.")}>Confirmer pertinente</button>
            <button className="ghost" type="button" onClick={() => void run(() => api(`/api/clarity/v1/adverse-media/alerts/${alert.id}/decision`, { method: "POST", body: JSON.stringify({ outcome: "false_positive", rationale }) }), "Faux positif enregistré.")}>Faux positif</button>
            <button className="ghost" type="button" onClick={() => void run(() => api(`/api/clarity/v1/adverse-media/alerts/${alert.id}/decision`, { method: "POST", body: JSON.stringify({ outcome: "closed", rationale }) }), "Alerte clôturée.")}>Clôturer</button>
          </div>
        </article>
      )) : <p className="label">Aucun résultat à revoir.</p>}
    </>
  );
}
