"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";

type Source = {
  id: string;
  name: string;
  kind: string;
  url: string;
  language: string;
  frequency_minutes: number;
  active: boolean;
  availability: string;
  last_error?: string | null;
  last_collected_at?: string | null;
  config?: { query?: string; quota_per_hour?: number; deep_search?: { status?: string; stored?: number; years?: number; error?: string } };
};

type Article = { id: string; title: string; url: string; status: string; language?: string | null; published_at?: string | null };
type ArticleDetail = Article & { versions: Array<{ version: number; nlp_status: string; nlp_engine_version?: string | null; analyzed_at?: string | null; error?: string | null; analysis?: { stance?: string; events?: Array<{ category: string; stance: string; negated: boolean }> } }> };
type AlertRow = { id: string; title: string; status: string; customer_id?: string | null; payload?: { stance?: string; category?: string; identity_score?: number; media_score?: number; excerpt?: string; article_title?: string } };
type Summary = { sources: number; active_sources: number; articles: number; pending_analyses: number; open_alerts: number; policy: { frequencies_hours: { high: number; medium: number; low: number }; identity_threshold: number; media_threshold: number } };
type SearchHit = { entity: string; identity_score: number; article_title: string; explanation: string[] };

export function AdverseMediaDesk() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [articles, setArticles] = useState<Article[]>([]);
  const [article, setArticle] = useState<ArticleDetail | null>(null);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [logs, setLogs] = useState<{ collections: Array<{ status: string; error?: string | null; stored: number; at: string }>; processing: Array<{ stage: string; status: string; error?: string | null; at: string }> } | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [rationale, setRationale] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [hours, setHours] = useState({ high: "24", medium: "168", low: "720" });
  const [deepYears, setDeepYears] = useState("3");

  async function load() {
    const [nextSummary, nextSources, nextArticles, nextAlerts, nextLogs] = await Promise.all([
      api<Summary>("/api/clarity/v1/adverse-media/summary"),
      api<Source[]>("/api/clarity/v1/adverse-media/sources"),
      api<Article[]>("/api/clarity/v1/adverse-media/articles"),
      api<AlertRow[]>("/api/clarity/v1/adverse-media/alerts"),
      api<NonNullable<typeof logs>>("/api/clarity/v1/adverse-media/logs"),
    ]);
    setSummary(nextSummary);
    setSources(nextSources);
    setArticles(nextArticles);
    setAlerts(nextAlerts);
    setLogs(nextLogs);
    setHours({
      high: String(nextSummary.policy.frequencies_hours.high),
      medium: String(nextSummary.policy.frequencies_hours.medium),
      low: String(nextSummary.policy.frequencies_hours.low),
    });
  }

  useEffect(() => {
    void load().catch((err: Error) => setError(err.message));
  }, []);

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

  async function saveSource(event: FormEvent, source: Source) {
    event.preventDefault();
    const form = new FormData(event.currentTarget as HTMLFormElement);
    await run(
      () => api(`/api/clarity/v1/adverse-media/sources/${source.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          url: String(form.get("url") || ""),
          frequency_minutes: Number(form.get("frequency") || source.frequency_minutes),
          active: form.get("active") === "on",
          config: { query: String(form.get("query") || "") },
        }),
      }),
      "Source enregistrée.",
    );
  }

  return (
    <>
      <h1>Adverse media</h1>
      <p className="lede">Collecte, analyse et alertes. Le moteur ne conclut pas à la culpabilité. Une source inactive ne crée aucun article.</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <div className="grid">
        <article className="card"><p className="label">Sources actives</p><strong>{summary?.active_sources ?? "—"} / {summary?.sources ?? "—"}</strong></article>
        <article className="card"><p className="label">Articles</p><strong>{summary?.articles ?? "—"}</strong></article>
        <article className="card"><p className="label">Analyses en attente</p><strong>{summary?.pending_analyses ?? "—"}</strong></article>
        <article className="card"><p className="label">Alertes ouvertes</p><strong>{summary?.open_alerts ?? "—"}</strong></article>
      </div>

      <article className="card">
        <h2>Recherche manuelle</h2>
        <form className="form" onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const body = await api<{ results: SearchHit[] }>("/api/clarity/v1/adverse-media/search", { method: "POST", body: JSON.stringify({ name: query }) });
            setHits(body.results);
          }, "Recherche terminée.");
        }}>
          <label>Nom<input value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <button type="submit">Rechercher</button>
        </form>
        {hits.map((hit) => <p key={`${hit.entity}-${hit.article_title}`}>{hit.entity} · identité {hit.identity_score} · {hit.article_title}</p>)}
      </article>

      <h2>Sources</h2>
      {sources.map((source) => (
        <form className="card form" key={source.id} onSubmit={(event) => void saveSource(event, source)}>
          <strong>{source.name}</strong>
          <p className="label">{source.kind} · {source.language} · {source.availability}{source.last_error ? ` · ${source.last_error}` : ""}</p>
          <label>URL<input name="url" defaultValue={source.url} /></label>
          {source.kind === "gdelt" ? <label>Requête GDELT<input name="query" defaultValue={source.config?.query || ""} /></label> : <input type="hidden" name="query" value={source.config?.query || ""} />}
          <label>Fréquence (minutes)<input name="frequency" type="number" min={15} defaultValue={source.frequency_minutes} /></label>
          <label><input name="active" type="checkbox" defaultChecked={source.active} /> Active</label>
          {source.kind === "gdelt" ? (
            <>
              <label>Recherche lointaine
                <select value={deepYears} onChange={(event) => setDeepYears(event.target.value)}>
                  <option value="1">1 an</option>
                  <option value="3">3 ans</option>
                  <option value="5">5 ans</option>
                </select>
              </label>
              <p className="label">Parcourt GDELT sur la période choisie. La requête est croisée avec corruption, fraude, blanchiment et terrorisme. Seuls les articles renvoyés sont enregistrés.</p>
            </>
          ) : null}
          <div className="actions">
            <button type="submit">Enregistrer</button>
            <button className="ghost" type="button" onClick={() => void run(() => api(`/api/clarity/v1/adverse-media/sources/${source.id}/collect`, { method: "POST" }), "Collecte lancée.")}>Collecter</button>
            {source.kind === "gdelt" ? (
              <button className="ghost" type="button" onClick={() => void run(() => api(`/api/clarity/v1/adverse-media/sources/${source.id}/deep-search`, { method: "POST", body: JSON.stringify({ years: Number(deepYears) }) }), "Recherche lointaine lancée. Les articles arrivent au fil de l'eau.")}>Recherche lointaine</button>
            ) : null}
          </div>
          {source.config?.deep_search?.status === "running" ? <p className="label">Recherche lointaine en cours.</p> : null}
          {source.config?.deep_search?.status === "completed" ? <p className="label">Recherche lointaine terminée · {source.config.deep_search.stored ?? 0} articles sur {source.config.deep_search.years} ans.</p> : null}
          {source.config?.deep_search?.status === "error" ? <p className="error">{source.config.deep_search.error || "Recherche lointaine interrompue."}</p> : null}
        </form>
      ))}

      <article className="card">
        <h2>Réévaluation des clients</h2>
        <p className="label">Heures entre deux criblages. Seuil d'identité {summary?.policy.identity_threshold ?? "—"}, seuil médiatique {summary?.policy.media_threshold ?? "—"}.</p>
        <form className="form" onSubmit={(event) => {
          event.preventDefault();
          void run(() => api("/api/clarity/v1/adverse-media/policy", { method: "PUT", body: JSON.stringify({ frequencies_hours: { high: Number(hours.high), medium: Number(hours.medium), low: Number(hours.low) } }) }), "Fréquences enregistrées.");
        }}>
          <label>Risque élevé<input value={hours.high} onChange={(event) => setHours({ ...hours, high: event.target.value })} /></label>
          <label>Risque moyen<input value={hours.medium} onChange={(event) => setHours({ ...hours, medium: event.target.value })} /></label>
          <label>Risque faible<input value={hours.low} onChange={(event) => setHours({ ...hours, low: event.target.value })} /></label>
          <button type="submit">Enregistrer</button>
        </form>
      </article>

      <h2>Articles</h2>
      <div className="grid">
        <article className="card">
          {articles.length ? articles.map((row) => (
            <button className="ghost" type="button" key={row.id} onClick={() => void api<ArticleDetail>(`/api/clarity/v1/adverse-media/articles/${row.id}`).then(setArticle).catch((err: Error) => setError(err.message))}>
              {row.title || row.url} · {row.status}
            </button>
          )) : <p className="label">Aucun article collecté.</p>}
        </article>
        <article className="card">
          {article ? (
            <>
              <h3>{article.title}</h3>
              <p className="label">{article.url}</p>
              {article.versions.map((version) => (
                <p key={version.version}>v{version.version} · {version.nlp_status} · {version.nlp_engine_version || "—"} · {version.analysis?.stance || "—"} {version.error ? `· ${version.error}` : ""}</p>
              ))}
            </>
          ) : <p className="label">Sélectionnez un article.</p>}
        </article>
      </div>

      <h2>Alertes</h2>
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
      )) : <p className="label">Aucune alerte.</p>}

      <h2>Journal</h2>
      <article className="card">
        {(logs?.collections || []).map((row, index) => <p key={`c-${index}`}>{row.at} · collecte {row.status} · {row.stored} article(s) {row.error ? `· ${row.error}` : ""}</p>)}
        {(logs?.processing || []).slice(0, 12).map((row, index) => <p key={`p-${index}`}>{row.at} · {row.stage} {row.status} {row.error ? `· ${row.error}` : ""}</p>)}
        {!logs?.collections.length && !logs?.processing.length ? <p className="label">Aucune tâche enregistrée.</p> : null}
      </article>
    </>
  );
}
