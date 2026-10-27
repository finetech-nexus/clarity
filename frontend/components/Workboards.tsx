"use client";

import Link from "next/link";
import { FormEvent, ReactNode, useEffect, useState } from "react";
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
  useEffect(() => {
    void searchPage<Row>("/api/clarity/v1/transactions", query, page).then((body) => { setRows(body.items); setTotal(body.total); });
  }, [query, page]);
  return (
    <Board title="Transactions" lede="Opérations ingérées. Une ligne ne constitue pas une alerte. La recherche filtre avant la page.">
      <QueryStack query={query} onChange={(next) => { setPage(0); setQuery(next); }} fields={[{ key: "name", label: "Client" }, { key: "country", label: "Pays" }, { key: "direction", label: "Sens", options: ["inbound", "outbound"] }, { key: "counterparty", label: "Contrepartie" }]} total={total} page={page} pageSize={25} onPage={setPage} />
      <table className="inbox">
        <thead><tr><th>Client</th><th>Montant</th><th>Sens</th><th>Pays</th><th>Contrepartie</th><th>Date</th></tr></thead>
        <tbody>
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

  async function load(nextQuery = query, nextPage = page) {
    const body = await searchPage<Row>("/api/clarity/v1/alerts", nextQuery, nextPage);
    setRows(body.items);
    setTotal(body.total);
  }
  useEffect(() => { void load(query, page).catch((err: Error) => setError(err.message)); }, [query, page]);

  async function decide(id: string, status: string) {
    setError("");
    try {
      await api(`/api/clarity/v1/alerts/${id}/disposition`, { method: "POST", body: JSON.stringify({ status, rationale: "Qualification manuelle." }) });
      setMessage(status === "escalated" ? "Alerte escaladée vers un dossier." : "Alerte qualifiée. Ce n'est pas une conclusion de blanchiment.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Qualification refusée");
    }
  }

  return (
    <Board title="Alertes" lede="Chaque alerte désigne un schéma à investiguer. Les filtres se cumulent.">
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <QueryStack query={query} onChange={(next) => { setPage(0); setQuery(next); }} fields={[{ key: "status", label: "Statut", options: ["open", "closed"] }, { key: "priority", label: "Priorité" }, { key: "name", label: "Client" }, { key: "title", label: "Titre" }]} total={total} page={page} pageSize={25} onPage={setPage} />
      <div className="list">
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
    </Board>
  );
}

export function ReviewBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => { void api<Row[]>("/api/clarity/v1/reviews").then(setRows); }, []);
  return (
    <Board title="Revues" lede="Tâches humaines encore ouvertes : revue analyste, contrôle, criblage ou investigation.">
      <table className="inbox">
        <thead><tr><th>Tâche</th><th>Étape</th><th>Échéance</th><th></th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.name)}</td>
              <td>{String(row.task_key)}</td>
              <td>{row.due_at ? new Date(String(row.due_at)).toLocaleDateString("fr-FR") : "—"}</td>
              <td><Link href={row.task_key === "investigate" ? "/investigations" : "/verification"}>{row.task_key === "investigate" ? "Ouvrir l'AML" : "Ouvrir le KYC"}</Link></td>
            </tr>
          ))}
        </tbody>
      </table>
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
  useEffect(() => { void api<Row[]>("/api/clarity/v1/audit").then(setRows); }, []);
  return (
    <Board title="Audit" lede="Journal des actions du tenant. Les secrets ne sont pas enregistrés.">
      <table className="inbox">
        <thead><tr><th>Quand</th><th>Acteur</th><th>Action</th><th>Objet</th></tr></thead>
        <tbody>
          {rows.slice(0, 40).map((row) => (
            <tr key={String(row.id)}>
              <td>{row.created_at ? new Date(String(row.created_at)).toLocaleString("fr-FR") : "—"}</td>
              <td>{String(row.actor)}</td>
              <td>{String(row.action)}</td>
              <td>{String(row.entity_type)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Board>
  );
}

const CONNECTORS = [
  { kind: "sanctions", title: "OpenSanctions", fields: [{ key: "api_key", label: "Clé API", secret: true }] },
  { kind: "identity", title: "Veriff", fields: [{ key: "api_key", label: "Clé API", secret: true }, { key: "shared_secret", label: "Secret partagé", secret: true }, { key: "base_url", label: "URL de base", secret: false }] },
  { kind: "language", title: "Modèle de langage", fields: [{ key: "api_key", label: "Clé API", secret: true }, { key: "base_url", label: "URL de base", secret: false }, { key: "model", label: "Modèle", secret: false }] },
] as const;

export function IntegrationBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [report, setReport] = useState<Row | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const [connections, compatibility] = await Promise.all([
      api<Row[]>("/api/clarity/v1/integrations"),
      api<Row>("/api/clarity/v1/compatibility"),
    ]);
    setRows(connections);
    setReport(compatibility);
  }

  useEffect(() => {
    void load().catch((err: Error) => setError(err.message));
  }, []);

  function value(kind: string, key: string) {
    return drafts[`${kind}.${key}`] || "";
  }

  async function save(event: FormEvent, kind: string) {
    event.preventDefault();
    setError("");
    const body: Record<string, string> = {};
    for (const [key, item] of Object.entries(drafts)) {
      if (key.startsWith(`${kind}.`) && item) body[key.slice(kind.length + 1)] = item;
    }
    try {
      await api(`/api/clarity/v1/integrations/${kind}`, { method: "PATCH", body: JSON.stringify(body) });
      setDrafts((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(`${kind}.`))));
      setMessage("Connecteur enregistré. La clé n'est pas renvoyée par l'API.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  const marble = (report?.marble_public || {}) as Row;
  const ballerine = (report?.ballerine || {}) as Row;
  const consoleReport = (report?.marble_console || {}) as Row;

  return (
    <Board title="Intégrations" lede="Les clés saisies ici remplacent les variables d'environnement vides. Un connecteur sans clé reste hors production.">
      {error ? <p className="error">{error}</p> : null}
      {message ? <p>{message}</p> : null}
      <div className="grid">
        {CONNECTORS.map((connector) => {
          const row = rows.find((item) => item.kind === connector.kind);
          return (
            <form className="card form" key={connector.kind} onSubmit={(event) => void save(event, connector.kind)}>
              <div className="label">{connector.kind}</div>
              <h2>{connector.title}</h2>
              <span className={row?.configured ? "tag" : "tag warn"}>{row?.has_secret ? "clé enregistrée" : "non configuré"}</span>
              {connector.fields.map((field) => (
                <label key={field.key}>
                  {field.label}
                  <input
                    type={field.secret ? "password" : "text"}
                    autoComplete="off"
                    placeholder={field.secret && row?.has_secret ? "laisser vide pour conserver" : ""}
                    value={value(connector.kind, field.key)}
                    onChange={(event) => setDrafts((current) => ({ ...current, [`${connector.kind}.${field.key}`]: event.target.value }))}
                  />
                </label>
              ))}
              <button type="submit">Enregistrer</button>
            </form>
          );
        })}
      </div>
      <article className="card">
        <h2>Remplacement des API</h2>
        <p>Marble public : {String(marble.expected || 0)} opérations, {marble.replaces ? "toutes servies" : `${(marble.missing as unknown[] | undefined)?.length || 0} manquante(s)`}.</p>
        <p>Console Marble : {String(consoleReport.expected || 0)} opérations, {consoleReport.replaces ? "toutes servies" : `${String(consoleReport.missing_count || 0)} manquante(s)`}.</p>
        <p>Ballerine : {String(ballerine.expected || 0)} opérations d'intégration, {ballerine.replaces ? "toutes servies" : `${(ballerine.missing as unknown[] | undefined)?.length || 0} manquante(s)`}.</p>
        <p>{String(report?.note || "")}</p>
      </article>
    </Board>
  );
}

export function AdminBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [me, setMe] = useState<Row | null>(null);
  const [error, setError] = useState("");
  const [years, setYears] = useState("1");
  const [saved, setSaved] = useState("");
  useEffect(() => {
    void api<Row>("/api/clarity/v1/me").then(setMe);
    void api<Row[]>("/api/clarity/v1/administration/users").then(setRows).catch((err: Error) => setError(err.message));
    void api<{ kyc_validity_years: number }>("/api/clarity/v1/administration/kyc").then((body) => setYears(String(body.kyc_validity_years))).catch(() => undefined);
  }, []);
  async function savePolicy(event: FormEvent) {
    event.preventDefault();
    setSaved("");
    setError("");
    try {
      const body = await api<{ kyc_validity_years: number }>("/api/clarity/v1/administration/kyc", {
        method: "PATCH",
        body: JSON.stringify({ kyc_validity_years: Number(years) }),
      });
      setYears(String(body.kyc_validity_years));
      setSaved("Durée de validité enregistrée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }
  async function assignRole(id: string, role: string) {
    setError("");
    try {
      const updated = await api<Row>(`/api/clarity/v1/administration/users/${id}`, { method: "PATCH", body: JSON.stringify({ role }) });
      setRows((current) => current.map((row) => (row.id === id ? { ...row, role: updated.role } : row)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Attribution refusée");
    }
  }
  return (
    <Board title="Administration" lede={me ? `Session ${String(me.name)} · ${String(me.role)}. Un compte OpenID reste en attente tant qu'un rôle ne lui est pas attribué.` : "Comptes du tenant."}>
      {error ? <p className="error">{error}</p> : null}
      {saved ? <p className="label">{saved}</p> : null}
      <form className="form" onSubmit={(event) => void savePolicy(event)}>
        <label>
          Validité du KYC (années)
          <input min={1} max={10} type="number" value={years} onChange={(event) => setYears(event.target.value)} />
        </label>
        <p className="label">Après approbation, la vérification expire à cette échéance. Le client peut alors en ouvrir une nouvelle, qui met à jour sa fiche.</p>
        <button type="submit">Enregistrer</button>
      </form>
      <table className="inbox">
        <thead><tr><th>Nom</th><th>E-mail</th><th>Rôle</th><th>MFA</th><th>Actif</th></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.name)}</td>
              <td>{String(row.email)}</td>
              <td>
                <select aria-label={`Rôle de ${String(row.name)}`} value={String(row.role)} onChange={(event) => void assignRole(String(row.id), event.target.value)}>
                  <option value="pending">En attente</option>
                  <option value="analyst">Analyste</option>
                  <option value="checker">Checker</option>
                  <option value="investigator">Investigateur</option>
                  <option value="admin">Administrateur</option>
                </select>
              </td>
              <td>{row.mfa_enabled ? "indiqué" : "non"}</td>
              <td>{row.active ? "oui" : "non"}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
