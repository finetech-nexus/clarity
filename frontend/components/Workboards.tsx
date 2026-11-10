"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FormEvent, ReactNode, useEffect, useState } from "react";
import DocumentCatalog from "./DocumentCatalog";
import { MenuAdmin } from "./MenuAdmin";
import { TeamAdmin } from "./TeamAdmin";
import { QueryStack } from "./QueryStack";
import { api, searchPage } from "../lib/api";

type Row = Record<string, unknown>;

export function RiskBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [selected, setSelected] = useState("");
  const [level, setLevel] = useState("2");
  const [rationale, setRationale] = useState("Dépassement justifié par le checker.");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);

  async function load(nextQuery = query, nextPage = page) {
    const body = await searchPage<Row>("/api/clarity/v1/risk", nextQuery, nextPage);
    setRows(body.items);
    setTotal(body.total);
    setSelected((current) => current || String(body.items[0]?.id || ""));
  }

  useEffect(() => {
    void load(query, page).catch((err: Error) => setError(err.message));
  }, [query, page]);

  const current = rows.find((row) => String(row.id) === selected) || rows[0];

  async function override(event: FormEvent) {
    event.preventDefault();
    if (!current) return;
    setError("");
    try {
      await api(`/api/clarity/v1/risk/${current.id}/override`, { method: "POST", body: JSON.stringify({ risk_level: Number(level), rationale }) });
      setMessage("Niveau remplacé. La source est override, pas un nouveau calcul.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dépassement refusé");
    }
  }

  return (
    <Board title="Risque" lede="Score courant par client. Un dépassement exige un checker et une justification.">
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <QueryStack query={query} onChange={(next) => { setPage(0); setQuery(next); }} fields={[{ key: "name", label: "Client" }, { key: "tier", label: "Niveau", options: ["low", "medium", "high"] }, { key: "source", label: "Source" }]} total={total} page={page} pageSize={25} onPage={setPage} />
      <div className="split">
        <table className="inbox">
          <thead><tr><th>Client</th><th>Score</th><th>Niveau</th><th>Source</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={String(row.id)} onClick={() => setSelected(String(row.id))}>
                <td>{String(row.customer_name || "—")}</td>
                <td>{String(row.score)}</td>
                <td>{String(row.tier)}</td>
                <td>{String(row.source)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {current ? (
          <article className="card">
            <h2>{String(current.customer_name || "Client")}</h2>
            <div className="metric">{String(current.score)}</div>
            <p className="label">{String(current.methodology_version)} · {String(current.rationale || "")}</p>
            {((current.factors as Row[]) || []).map((factor, index) => (
              <p className="factor" key={index}>{String(factor.points)} pts — {String(factor.explanation)}</p>
            ))}
            <form className="form" onSubmit={override}>
              <input aria-label="Niveau" value={level} onChange={(event) => setLevel(event.target.value)} />
              <input className="wide" aria-label="Justification" value={rationale} onChange={(event) => setRationale(event.target.value)} />
              <button type="submit">Dépasser le score</button>
            </form>
          </article>
        ) : null}
      </div>
    </Board>
  );
}

export function TransactionBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const pageSize = 20;
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void searchPage<Row>("/api/clarity/v1/transactions", query, page, pageSize)
      .then((body) => {
        if (cancelled) return;
        setTotal(body.total);
        setRows((current) => {
          if (page === 0) return body.items;
          const seen = new Set(current.map((row) => String(row.id)));
          return [...current, ...body.items.filter((row) => !seen.has(String(row.id)))];
        });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [query, page]);
  return (
    <Board title="Transactions" lede="Opérations ingérées. Une ligne ne constitue pas une alerte. Chargement progressif pour alléger la page.">
      <QueryStack
        query={query}
        onChange={(next) => {
          setPage(0);
          setRows([]);
          setQuery(next);
        }}
        fields={[{ key: "name", label: "Client" }, { key: "country", label: "Pays" }, { key: "direction", label: "Sens", options: ["inbound", "outbound"] }, { key: "counterparty", label: "Contrepartie" }]}
        total={total}
        page={0}
        pageSize={Math.max(total, 1)}
        onPage={() => undefined}
      />
      <table className="inbox">
        <thead><tr><th>Client</th><th>Montant</th><th>Sens</th><th>Pays</th><th>Contrepartie</th><th>Date</th></tr></thead>
        <tbody>
          {!rows.length ? <tr><td colSpan={6}>{loading ? "Chargement…" : "Aucune transaction."}</td></tr> : null}
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.customer_name || "—")}</td>
              <td>{String(row.amount)} {String(row.currency)}</td>
              <td>{String(row.direction)}</td>
              <td>{String(row.country)}</td>
              <td>{String(row.counterparty_name)}</td>
              <td>{row.occurred_at ? new Date(String(row.occurred_at)).toLocaleString("fr-FR") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="label">{rows.length}/{total} transaction(s)</p>
      {rows.length < total ? (
        <div className="load-more">
          <button className="ghost" type="button" disabled={loading} onClick={() => setPage((current) => current + 1)}>
            {loading ? "Chargement…" : "Charger plus"}
          </button>
        </div>
      ) : null}
    </Board>
  );
}

export function AlertBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("status:open");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const pageSize = 20;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void searchPage<Row>("/api/clarity/v1/alerts", query, page, pageSize)
      .then((body) => {
        if (cancelled) return;
        setTotal(body.total);
        setRows((current) => {
          if (page === 0) return body.items;
          const seen = new Set(current.map((row) => String(row.id)));
          return [...current, ...body.items.filter((row) => !seen.has(String(row.id)))];
        });
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [query, page, reload]);

  async function decide(id: string, status: string) {
    setError("");
    try {
      await api(`/api/clarity/v1/alerts/${id}/disposition`, { method: "POST", body: JSON.stringify({ status, rationale: "Qualification manuelle." }) });
      setMessage(status === "escalated" ? "Alerte escaladée vers un dossier." : "Alerte qualifiée. Ce n'est pas une conclusion de blanchiment.");
      setRows([]);
      setPage(0);
      setReload((current) => current + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Qualification refusée");
    }
  }

  return (
    <Board title="Alertes" lede="Chaque alerte désigne un schéma à investiguer. Les filtres se cumulent. Chargement par bloc.">
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <QueryStack
        query={query}
        onChange={(next) => { setPage(0); setRows([]); setQuery(next); }}
        fields={[{ key: "status", label: "Statut", options: ["open", "closed"] }, { key: "priority", label: "Priorité" }, { key: "name", label: "Client" }, { key: "title", label: "Titre" }]}
        total={total}
        page={0}
        pageSize={Math.max(total, 1)}
        onPage={() => undefined}
      />
      <div className="list">
        {!rows.length ? <p className="label">{loading ? "Chargement…" : "Aucune alerte."}</p> : null}
        {rows.map((row) => (
          <div className="row" key={String(row.id)}>
            <span>
              {String(row.title)}
              <div className="label">{String(row.customer_name || "")} · {String(row.rationale || "")}</div>
            </span>
            {row.status === "open" && !row.disposition ? (
              <span className="tabs">
                <button type="button" onClick={() => void decide(String(row.id), "false_positive")}>Faux positif</button>
                <button className="ghost" type="button" onClick={() => void decide(String(row.id), "valuable_alert")}>Utile</button>
                <button className="ghost" type="button" onClick={() => void decide(String(row.id), "escalated")}>Escalader</button>
              </span>
            ) : <span className="tag">{String(row.disposition || row.status)}</span>}
          </div>
        ))}
      </div>
      <p className="label">{rows.length}/{total} alerte(s)</p>
      {rows.length < total ? (
        <div className="load-more">
          <button className="ghost" type="button" disabled={loading} onClick={() => setPage((current) => current + 1)}>
            {loading ? "Chargement…" : "Charger plus"}
          </button>
        </div>
      ) : null}
    </Board>
  );
}

type ReviewRow = {
  id: string;
  task_key: string;
  name: string;
  status: string;
  due_at?: string | null;
  created_at?: string | null;
  assignee?: string | null;
  customer?: { id: string; name: string; kind: string; status: string; risk_tier?: string | null } | null;
  workflow?: { name: string; key: string; state?: string | null; status: string; runtime_id?: string | null } | null;
  scenario?: string | null;
  entity_type?: string | null;
  nationality?: string | null;
  documents?: number;
  risk?: { score: number; tier: string } | null;
  case?: { id: string; name: string; status: string; outcome: string } | null;
};

function reviewHref(row: ReviewRow) {
  if (row.task_key === "investigate") return "/investigations";
  const runtime = row.workflow?.runtime_id;
  const base = row.workflow?.key === "clarityKybBusiness" || row.customer?.kind === "business" ? "/kyb-verification" : "/verification";
  return runtime ? `${base}?case=${encodeURIComponent(runtime)}` : base;
}

export function ReviewBoard() {
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [selected, setSelected] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const pageSize = 20;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void searchPage<ReviewRow>("/api/clarity/v1/reviews", "", page, pageSize)
      .then((body) => {
        if (cancelled) return;
        setTotal(body.total);
        setRows((current) => {
          const next = page === 0
            ? body.items
            : [...current, ...body.items.filter((row) => !current.some((item) => item.id === row.id))];
          setSelected((chosen) => chosen || next[0]?.id || "");
          return next;
        });
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [page]);

  const current = rows.find((row) => row.id === selected) || rows[0];
  return (
    <Board title="Revues" lede="Tâches humaines encore ouvertes. Chargement par bloc.">
      {error ? <p className="error">{error}</p> : null}
      <div className="editor">
        <div className="queue">
          {!rows.length ? <p className="label">{loading ? "Chargement…" : "Aucune revue ouverte."}</p> : null}
          {rows.map((row) => (
            <button className={current?.id === row.id ? "active" : "ghost"} key={row.id} type="button" onClick={() => setSelected(row.id)}>
              <span>{row.customer?.name || row.name}</span>
              <span className="label">{row.name} · {row.due_at ? new Date(row.due_at).toLocaleDateString("fr-FR") : "sans échéance"}</span>
            </button>
          ))}
          <p className="label">{rows.length}/{total}</p>
          {rows.length < total ? (
            <button className="ghost" type="button" disabled={loading} onClick={() => setPage((value) => value + 1)}>
              {loading ? "Chargement…" : "Charger plus"}
            </button>
          ) : null}
        </div>
        {current ? (
          <section className="case-sheet">
            <header className="case-bar">
              <div>
                <h2>{current.customer?.name || current.name}</h2>
                <p className="label">{current.name} · {current.workflow?.name || current.task_key}</p>
              </div>
              <Link href={reviewHref(current)}>{current.task_key === "investigate" ? "Ouvrir l'investigation" : "Ouvrir la vérification"}</Link>
            </header>
            <dl className="kv">
              <dt>Client</dt><dd>{current.customer?.name || "—"}</dd>
              <dt>Type</dt><dd>{current.customer?.kind === "business" || current.entity_type === "business" ? "Personne morale" : current.customer ? "Personne physique" : "—"}</dd>
              <dt>Statut client</dt><dd>{current.customer?.status || "—"}</dd>
              <dt>Parcours</dt><dd>{current.workflow?.name || "—"}</dd>
              <dt>Étape du parcours</dt><dd>{current.workflow?.state || "—"}</dd>
              <dt>Tâche</dt><dd>{current.name} ({current.task_key})</dd>
              <dt>Statut de la tâche</dt><dd>{current.status}</dd>
              <dt>Ouverte le</dt><dd>{current.created_at ? new Date(current.created_at).toLocaleString("fr-FR") : "—"}</dd>
              <dt>Échéance</dt><dd>{current.due_at ? new Date(current.due_at).toLocaleDateString("fr-FR") : "—"}</dd>
              <dt>Assigné</dt><dd>{current.assignee || "Non assigné"}</dd>
              <dt>Scénario</dt><dd>{current.scenario || "—"}</dd>
              <dt>Nationalité</dt><dd>{current.nationality || "—"}</dd>
              <dt>Pièces</dt><dd>{current.documents ?? 0}</dd>
              <dt>Score</dt><dd>{current.risk ? `${current.risk.score} · ${current.risk.tier}` : current.customer?.risk_tier || "—"}</dd>
              <dt>Dossier</dt><dd>{current.case ? `${current.case.name} · ${current.case.status}` : "—"}</dd>
            </dl>
          </section>
        ) : null}
      </div>
    </Board>
  );
}

export function ReportBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => { void api<Row[]>("/api/clarity/v1/reports").then(setRows); }, []);
  return (
    <Board title="Rapports" lede="Préparations internes. Aucun dépôt TRACFIN n'est connecté.">
      {!rows.length ? <p className="label">Aucune déclaration préparée. Elle se rédige depuis le dossier d'investigation.</p> : null}
      <table className="inbox">
        <thead><tr><th>Type</th><th>Statut</th><th>Canal</th><th>Date</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.type)}</td>
              <td>{String(row.status)}</td>
              <td>{String(row.submission_channel)}</td>
              <td>{row.created_at ? new Date(String(row.created_at)).toLocaleString("fr-FR") : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Board>
  );
}

export function AuditBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [entity, setEntity] = useState("");
  const [when, setWhen] = useState("");
  const [error, setError] = useState("");
  const pageSize = 25;

  useEffect(() => {
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize) });
    if (actor.trim()) params.set("actor", actor.trim());
    if (action.trim()) params.set("action", action.trim());
    if (entity.trim()) params.set("entity", entity.trim());
    if (when.trim()) params.set("when", when.trim());
    void api<{ items: Row[]; total: number }>(`/api/clarity/v1/audit?${params}`)
      .then((body) => {
        setRows(body.items);
        setTotal(body.total);
      })
      .catch((err: Error) => setError(err.message));
  }, [actor, action, entity, when, page]);

  function filter(setter: (value: string) => void) {
    return (value: string) => {
      setPage(0);
      setter(value);
    };
  }

  return (
    <div className="stack">
      <p className="lede">Journal des actions du tenant. Les secrets ne sont pas enregistrés.</p>
      {error ? <p className="error">{error}</p> : null}
      <table className="inbox sheet">
        <thead>
          <tr>
            <th>Quand<input aria-label="Filtrer la date" value={when} onChange={(event) => filter(setWhen)(event.target.value)} placeholder="2026-10" /></th>
            <th>Acteur<input aria-label="Filtrer l'acteur" value={actor} onChange={(event) => filter(setActor)(event.target.value)} /></th>
            <th>Action<input aria-label="Filtrer l'action" value={action} onChange={(event) => filter(setAction)(event.target.value)} /></th>
            <th>Objet<input aria-label="Filtrer l'objet" value={entity} onChange={(event) => filter(setEntity)(event.target.value)} /></th>
          </tr>
        </thead>
        <tbody>
          {!rows.length ? <tr><td colSpan={4}>Aucun événement.</td></tr> : null}
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{row.created_at ? new Date(String(row.created_at)).toLocaleString("fr-FR") : "—"}</td>
              <td>{String(row.actor)}</td>
              <td>{String(row.action)}</td>
              <td>{String(row.entity_type)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="label">{total} événement(s) · page {page + 1}</p>
      <div className="tabs">
        <button className="ghost" type="button" disabled={page === 0} onClick={() => setPage((current) => Math.max(0, current - 1))}>Précédent</button>
        <button className="ghost" type="button" disabled={(page + 1) * pageSize >= total} onClick={() => setPage((current) => current + 1)}>Suivant</button>
      </div>
    </div>
  );
}

const PURPOSES = [
  ["sanctions", "Sanctions"],
  ["identity", "Identité"],
  ["adverse_media", "Adverse media"],
] as const;

type ProviderField = { key: string; label: string; secret: boolean };
type ProviderSpec = { id: string; purpose: string; name: string; hint: string; fields: ProviderField[] };
type ScreeningSource = {
  id: string;
  name: string;
  level: string;
  detail: string;
  covered: boolean;
  enabled: boolean;
};
type MediaSource = { id: string; name: string; family: string; language: string; enabled: boolean };
type MediaBatch = { status: string; progress: number; done: number; total: number; stored: number; message: string };

export function IntegrationBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [catalog, setCatalog] = useState<ProviderSpec[]>([]);
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [sources, setSources] = useState<ScreeningSource[]>([]);
  const [mediaSources, setMediaSources] = useState<MediaSource[]>([]);
  const [batch, setBatch] = useState<MediaBatch>({ status: "idle", progress: 0, done: 0, total: 0, stored: 0, message: "" });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const [connections, providers] = await Promise.all([
      api<Row[]>("/api/clarity/v1/integrations"),
      api<ProviderSpec[]>("/api/clarity/v1/integrations/providers"),
    ]);
    setRows(connections);
    setCatalog(providers);
    const sanctions = connections.find((item) => item.kind === "sanctions");
    const listed = Array.isArray(sanctions?.sources) ? (sanctions.sources as ScreeningSource[]) : [];
    setSources(listed);
    const media = connections.find((item) => item.kind === "adverse_media");
    setMediaSources(Array.isArray(media?.sources) ? (media.sources as MediaSource[]) : []);
    const nextBatch = media?.batch as MediaBatch | undefined;
    if (nextBatch) setBatch(nextBatch);
  }

  useEffect(() => {
    if (batch.status !== "running") return;
    const timer = window.setInterval(() => {
      void api<MediaBatch>("/api/clarity/v1/adverse-media/batch").then(setBatch).catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [batch.status]);

  useEffect(() => {
    void load().catch((err: Error) => setError(err.message));
  }, []);

  function value(kind: string, key: string) {
    return drafts[`${kind}.${key}`] || "";
  }

  function providerFor(kind: string) {
    const options = catalog.filter((item) => item.purpose === kind);
    const row = rows.find((item) => item.kind === kind);
    const selected = chosen[kind] || String(row?.provider || options[0]?.id || "");
    return { options, provider: options.find((item) => item.id === selected) || options[0], row };
  }

  async function save(event: FormEvent, kind: string) {
    event.preventDefault();
    setError("");
    const { provider } = providerFor(kind);
    if (!provider) return;
    const body: Record<string, unknown> = { provider: provider.id };
    for (const [key, item] of Object.entries(drafts)) {
      if (key.startsWith(`${kind}.`) && item) body[key.slice(kind.length + 1)] = item;
    }
    if (kind === "sanctions") {
      const master = sources.find((source) => source.id === "opensanctions");
      body.enabled = master ? master.enabled : true;
      body.sources = Object.fromEntries(
        sources.filter((source) => source.id !== "opensanctions").map((source) => [source.id, source.enabled])
      );
    }
    if (kind === "adverse_media") {
      body.sources = Object.fromEntries(mediaSources.map((source) => [source.id, source.enabled]));
    }
    try {
      await api(`/api/clarity/v1/integrations/${kind}`, { method: "PATCH", body: JSON.stringify(body) });
      const checked = await api<Row>(`/api/clarity/v1/integrations/${kind}/check`, { method: "POST" });
      setDrafts((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(`${kind}.`))));
      setMessage(String(checked.connection_message || "Connecteur enregistré."));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function check(kind: string) {
    setError("");
    try {
      const checked = await api<Row>(`/api/clarity/v1/integrations/${kind}/check`, { method: "POST" });
      setMessage(String(checked.connection_message || ""));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vérification impossible");
    }
  }

  return (
    <div className="stack">
      <p className="lede">Pour chaque usage, choisissez un fournisseur dans la liste connue. La clé enregistrée n'est pas renvoyée. Les sources d'adverse media se règlent ici. Le batch affiche seulement sa progression.</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p>{message}</p> : null}
      <div className="connectors">
        {PURPOSES.map(([kind, title]) => {
          const { options, provider, row } = providerFor(kind);
          if (!provider) return null;
          const connected = row?.connection_status === "connected";
          const rejected = row?.connection_status === "rejected";
          const status = connected ? "Connecté" : rejected ? "Refusé" : row?.has_secret ? "Clé enregistrée" : "Non configuré";
          return (
            <form className="card connector" key={kind} onSubmit={(event) => void save(event, kind)}>
              <header>
                <div>
                  <div className="label">{title}</div>
                  <h2>{provider.name}</h2>
                </div>
                <span className={connected ? "tag" : "tag warn"}>{status}</span>
              </header>
              <label>
                Fournisseur
                <select value={provider.id} onChange={(event) => setChosen((current) => ({ ...current, [kind]: event.target.value }))}>
                  {options.map((option) => (
                    <option key={option.id} value={option.id}>{option.name}</option>
                  ))}
                </select>
              </label>
              {provider.fields.map((field) => (
                <label key={field.key}>
                  {field.label}
                  <input
                    type={field.secret ? "password" : "text"}
                    autoComplete="off"
                    placeholder={field.secret && row?.has_secret ? "laisser vide pour conserver" : ""}
                    value={value(kind, field.key)}
                    onChange={(event) => setDrafts((current) => ({ ...current, [`${kind}.${field.key}`]: event.target.value }))}
                  />
                </label>
              ))}
              <p>{provider.hint}</p>
              {kind === "sanctions" && provider.id === "opensanctions" && sources.length ? (
                <fieldset className="stack">
                  <legend>Contrôles vrai / faux</legend>
                  {sources.map((source) => (
                    <label key={source.id}>
                      <input
                        type="checkbox"
                        checked={source.enabled}
                        onChange={(event) =>
                          setSources((current) =>
                            current.map((item) => (item.id === source.id ? { ...item, enabled: event.target.checked } : item))
                          )
                        }
                      />
                      {source.name}
                      <span className="label">
                        {" "}
                        · {source.level}
                        {source.id !== "opensanctions" && !source.covered ? " · pas de liste OpenSanctions publiée" : ""}
                      </span>
                      <div className="label">{source.detail}</div>
                    </label>
                  ))}
                </fieldset>
              ) : null}
              {kind === "adverse_media" && provider.id === "public_media" && mediaSources.length ? (
                <fieldset className="stack">
                  <legend>Sources</legend>
                  {mediaSources.map((source) => (
                    <label key={source.id}>
                      <input
                        type="checkbox"
                        checked={source.enabled}
                        onChange={(event) =>
                          setMediaSources((current) =>
                            current.map((item) => (item.id === source.id ? { ...item, enabled: event.target.checked } : item))
                          )
                        }
                      />
                      {source.name}
                      <span className="label"> · {source.family} · {source.language}</span>
                    </label>
                  ))}
                </fieldset>
              ) : null}
              {kind === "adverse_media" ? (
                <div>
                  <p className="label">Batch {batch.progress}% · {batch.done}/{batch.total} · {batch.message || batch.status}</p>
                  <div style={{ height: 8, background: "#e6e6e6" }}>
                    <div style={{ width: `${Math.max(0, Math.min(batch.progress, 100))}%`, height: 8, background: "#1f6b4a" }} />
                  </div>
                </div>
              ) : null}
              {row?.connection_message ? <p>{String(row.connection_message)}</p> : null}
              <div className="actions">
                <button type="submit">Enregistrer</button>
                <button type="button" className="ghost" onClick={() => void check(kind)}>Vérifier</button>
                {kind === "adverse_media" && provider.id === "public_media" ? (
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => {
                      setBatch((current) => ({ ...current, status: "running", progress: 0, message: "Batch en cours." }));
                      void api<MediaBatch>("/api/clarity/v1/adverse-media/batch", { method: "POST" })
                        .then((next) => setBatch(next))
                        .catch((err: Error) => setError(err.message));
                    }}
                  >
                    Lancer le batch
                  </button>
                ) : null}
              </div>
            </form>
          );
        })}
      </div>
    </div>
  );
}

type StorageConfig = {
  provider: "filesystem" | "s3" | "minio";
  bucket: string;
  access_key: string;
  secret_key: string;
  endpoint: string;
  region: string;
  force_path_style: boolean;
};

const EMPTY_STORAGE: StorageConfig = {
  provider: "minio",
  bucket: "ballerine-documents",
  access_key: "",
  secret_key: "",
  endpoint: "https://minio.payment-hub.dev.sandbox.internal.nbank.fr",
  region: "",
  force_path_style: true,
};

const ADMIN_TABS = ["equipe", "config", "stockage", "menu", "documents", "thirdparty", "audit"] as const;
type AdminTab = (typeof ADMIN_TABS)[number];

export function AdminBoard() {
  const requested = useSearchParams().get("tab");
  const [tab, setTab] = useState<AdminTab>(ADMIN_TABS.includes(requested as AdminTab) ? (requested as AdminTab) : "equipe");
  const [me, setMe] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [years, setYears] = useState("1");
  const [makerChecker, setMakerChecker] = useState(true);
  const [threshold, setThreshold] = useState("40");
  const [saved, setSaved] = useState("");
  const [makerSaved, setMakerSaved] = useState("");
  const [thresholdSaved, setThresholdSaved] = useState("");
  const [storage, setStorage] = useState<StorageConfig>(EMPTY_STORAGE);
  const [storageSaved, setStorageSaved] = useState("");
  useEffect(() => {
    void api<Row>("/api/clarity/v1/me").then(setMe);
    void api<{ kyc_validity_years: number; maker_checker_enforced?: boolean }>("/api/clarity/v1/administration/kyc")
      .then((body) => {
        setYears(String(body.kyc_validity_years));
        setMakerChecker(body.maker_checker_enforced !== false);
      })
      .catch(() => undefined);
    void api<{ threshold: number }>("/api/clarity/v1/administration/face-threshold").then((body) => setThreshold(String(body.threshold))).catch(() => undefined);
    void api<StorageConfig>("/api/clarity/v1/administration/storage")
      .then((body) => setStorage({
        provider: body.provider === "s3" || body.provider === "filesystem" ? body.provider : "minio",
        bucket: body.bucket || EMPTY_STORAGE.bucket,
        access_key: body.access_key || "",
        secret_key: body.secret_key || "",
        endpoint: body.endpoint || EMPTY_STORAGE.endpoint,
        region: body.region || "",
        force_path_style: body.force_path_style !== false,
      }))
      .catch(() => undefined);
  }, []);
  async function savePolicy(event: FormEvent) {
    event.preventDefault();
    setSaved("");
    setError("");
    try {
      const body = await api<{ kyc_validity_years: number; maker_checker_enforced: boolean }>("/api/clarity/v1/administration/kyc", {
        method: "PATCH",
        body: JSON.stringify({ kyc_validity_years: Number(years) }),
      });
      setYears(String(body.kyc_validity_years));
      setMakerChecker(body.maker_checker_enforced !== false);
      setSaved("Durée de validité enregistrée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }
  async function saveMakerChecker(event: FormEvent) {
    event.preventDefault();
    setMakerSaved("");
    setError("");
    try {
      const body = await api<{ kyc_validity_years: number; maker_checker_enforced: boolean }>("/api/clarity/v1/administration/kyc", {
        method: "PATCH",
        body: JSON.stringify({ maker_checker_enforced: makerChecker }),
      });
      setMakerChecker(body.maker_checker_enforced !== false);
      setMakerSaved(body.maker_checker_enforced ? "Séparation maker / checker activée." : "Séparation maker / checker désactivée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }
  async function saveThreshold(event: FormEvent) {
    event.preventDefault();
    setThresholdSaved("");
    setError("");
    try {
      const body = await api<{ threshold: number }>("/api/clarity/v1/administration/face-threshold", {
        method: "PATCH",
        body: JSON.stringify({ threshold: Number(threshold) }),
      });
      setThreshold(String(body.threshold));
      setThresholdSaved("Seuil de correspondance enregistré.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }
  async function saveStorage(event: FormEvent) {
    event.preventDefault();
    setStorageSaved("");
    setError("");
    try {
      const body = await api<StorageConfig>("/api/clarity/v1/administration/storage", {
        method: "PATCH",
        body: JSON.stringify(storage),
      });
      setStorage({
        provider: body.provider === "s3" || body.provider === "filesystem" ? body.provider : "minio",
        bucket: body.bucket || "",
        access_key: body.access_key || "",
        secret_key: body.secret_key || "",
        endpoint: body.endpoint || "",
        region: body.region || "",
        force_path_style: body.force_path_style !== false,
      });
      setStorageSaved("Stockage enregistré.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }
  return (
    <Board title="Administration" lede={me ? `Session ${String(me.name)}.` : "Équipe et configuration."}>
      <div className="tabs">
        <button className={tab === "equipe" ? "active" : ""} type="button" onClick={() => setTab("equipe")}>Équipe</button>
        <button className={tab === "config" ? "active" : ""} type="button" onClick={() => setTab("config")}>Configuration globale</button>
        <button className={tab === "stockage" ? "active" : ""} type="button" onClick={() => setTab("stockage")}>Stockage</button>
        <button className={tab === "menu" ? "active" : ""} type="button" onClick={() => setTab("menu")}>Menu</button>
        <button className={tab === "documents" ? "active" : ""} type="button" onClick={() => setTab("documents")}>Documents</button>
        <button className={tab === "thirdparty" ? "active" : ""} type="button" onClick={() => setTab("thirdparty")}>Third party</button>
        <button className={tab === "audit" ? "active" : ""} type="button" onClick={() => setTab("audit")}>Audit</button>
      </div>
      {error ? <p className="error">{error}</p> : null}
      {tab === "menu" ? <MenuAdmin /> : null}
      {tab === "documents" ? <DocumentCatalog /> : null}
      {tab === "thirdparty" ? <IntegrationBoard /> : null}
      {tab === "audit" ? <AuditBoard /> : null}
      {tab === "stockage" ? (
        <form className="form card" onSubmit={(event) => void saveStorage(event)}>
          <h3>Stockage des pièces</h3>
          {storageSaved ? <p className="label">{storageSaved}</p> : null}
          <label>
            Fournisseur
            <select
              value={storage.provider}
              onChange={(event) => {
                const provider = event.target.value as StorageConfig["provider"];
                setStorage((current) => ({
                  ...current,
                  provider,
                  ...(provider === "minio" && !current.endpoint
                    ? { endpoint: EMPTY_STORAGE.endpoint, bucket: current.bucket || EMPTY_STORAGE.bucket, force_path_style: true }
                    : {}),
                  ...(provider === "filesystem" ? { bucket: "", endpoint: "", access_key: "", secret_key: "", region: "" } : {}),
                }));
              }}
            >
              <option value="filesystem">Disque local</option>
              <option value="minio">MinIO</option>
              <option value="s3">Amazon S3</option>
            </select>
          </label>
          {storage.provider !== "filesystem" ? (
            <>
              <label>
                Bucket
                <input value={storage.bucket} onChange={(event) => setStorage((current) => ({ ...current, bucket: event.target.value }))} required />
              </label>
              <label>
                Clé d&apos;accès
                <input type="text" value={storage.access_key} onChange={(event) => setStorage((current) => ({ ...current, access_key: event.target.value }))} autoComplete="off" spellCheck={false} required />
              </label>
              <label>
                Secret
                <input type="text" value={storage.secret_key} onChange={(event) => setStorage((current) => ({ ...current, secret_key: event.target.value }))} autoComplete="off" spellCheck={false} required />
              </label>
              <label>
                Endpoint
                <input value={storage.endpoint} onChange={(event) => setStorage((current) => ({ ...current, endpoint: event.target.value }))} placeholder="https://minio…" required={storage.provider === "minio"} />
              </label>
              <label>
                Région
                <input value={storage.region} onChange={(event) => setStorage((current) => ({ ...current, region: event.target.value }))} placeholder="optionnel" />
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={storage.force_path_style}
                  onChange={(event) => setStorage((current) => ({ ...current, force_path_style: event.target.checked }))}
                />
                Path-style (requis pour MinIO)
              </label>
            </>
          ) : (
            <p className="label">Les fichiers restent sur le disque de l&apos;API tant qu&apos;aucun S3 ou MinIO n&apos;est enregistré.</p>
          )}
          <button type="submit">Enregistrer</button>
        </form>
      ) : null}
      {tab === "config" ? (
        <div className="action-grid">
          <form className="form card" onSubmit={(event) => void saveMakerChecker(event)}>
            <h3>Contrôle « La personne qui a instruit le dossier ne peut pas l&apos;approuver »</h3>
            {makerSaved ? <p className="label">{makerSaved}</p> : null}
            <label className="check">
              <input
                type="checkbox"
                checked={makerChecker}
                onChange={(event) => setMakerChecker(event.target.checked)}
              />
              Activer la séparation maker / checker
            </label>
            <p className="label">Coché : l&apos;analyste qui transmet ne peut pas valider en contrôle. Décoché : la même personne peut instruire puis approuver.</p>
            <button type="submit">Enregistrer</button>
          </form>
          <form className="form card" onSubmit={(event) => void savePolicy(event)}>
            <h3>Validité du KYC</h3>
            {saved ? <p className="label">{saved}</p> : null}
            <label>
              Années
              <input min={1} max={10} type="number" value={years} onChange={(event) => setYears(event.target.value)} />
            </label>
            <p className="label">Après approbation, la vérification expire à cette échéance. Une nouvelle vérification est alors créée pour mettre à jour le client.</p>
            <button type="submit">Enregistrer</button>
          </form>
          <form className="form card" onSubmit={(event) => void saveThreshold(event)}>
            <h3>Correspondance faciale</h3>
            {thresholdSaved ? <p className="label">{thresholdSaved}</p> : null}
            <label>
              Seuil OK
              <input aria-label="Seuil de correspondance faciale" min={0} max={100} type="number" value={threshold} onChange={(event) => setThreshold(event.target.value)} />
            </label>
            <p className="label">Score en dessous du seuil : KO. À partir de ce score : OK. La vérification affiche ce seuil, elle ne le modifie pas.</p>
            <button type="submit">Enregistrer</button>
          </form>
        </div>
      ) : null}
      {tab === "equipe" ? <TeamAdmin /> : null}
    </Board>
  );
}

function Board({ title, lede, children }: { title: string; lede: string; children: ReactNode }) {
  return (
    <>
      <h1>{title}</h1>
      <p className="lede">{lede}</p>
      {children}
    </>
  );
}
