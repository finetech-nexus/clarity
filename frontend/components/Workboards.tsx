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
    <Board title="Audit" lede="Journal des actions du tenant. Les secrets ne sont pas enregistrés.">
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
    </Board>
  );
}

const PURPOSES = [
  ["sanctions", "Sanctions"],
  ["identity", "Identité"],
] as const;

type ProviderField = { key: string; label: string; secret: boolean };
type ProviderSpec = { id: string; purpose: string; name: string; hint: string; fields: ProviderField[] };

export function IntegrationBoard() {
  const [rows, setRows] = useState<Row[]>([]);
  const [catalog, setCatalog] = useState<ProviderSpec[]>([]);
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const [connections, providers] = await Promise.all([
      api<Row[]>("/api/clarity/v1/integrations"),
      api<ProviderSpec[]>("/api/clarity/v1/integrations/providers"),
    ]);
    setRows(connections);
    setCatalog(providers);
  }

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
    const body: Record<string, string> = { provider: provider.id };
    for (const [key, item] of Object.entries(drafts)) {
      if (key.startsWith(`${kind}.`) && item) body[key.slice(kind.length + 1)] = item;
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
    <Board title="Intégrations" lede="Pour chaque usage, choisissez un fournisseur dans la liste connue. La clé enregistrée n'est pas renvoyée. Sans clé, le criblage reste sur la liste de démonstration.">
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
              {row?.connection_message ? <p>{String(row.connection_message)}</p> : null}
              <div className="actions">
                <button type="submit">Enregistrer</button>
                <button type="button" className="ghost" onClick={() => void check(kind)}>Vérifier</button>
              </div>
            </form>
          );
        })}
      </div>
    </Board>
  );
}

const TEAM_ROLES = [
  ["analyst", "Analyste"],
  ["checker", "Checker"],
  ["investigator", "Investigateur"],
  ["admin", "Administrateur"],
] as const;

function roleList(row: Row): string[] {
  if (Array.isArray(row.roles)) return row.roles.map(String);
  return String(row.role || "").split(",").map((item) => item.trim()).filter(Boolean);
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

export function AdminBoard() {
  const [tab, setTab] = useState<"equipe" | "config" | "stockage" | "donnees">("equipe");
  const [rows, setRows] = useState<Row[]>([]);
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
  const [givenName, setGivenName] = useState("");
  const [familyName, setFamilyName] = useState("");
  const [email, setEmail] = useState("");
  const [memberPassword, setMemberPassword] = useState("");
  const [passwordMode, setPasswordMode] = useState(false);
  const [chosen, setChosen] = useState<string[]>(["analyst"]);
  useEffect(() => {
    void api<Row>("/api/clarity/v1/me").then(setMe);
    void api<Row[]>("/api/clarity/v1/administration/users").then(setRows).catch((err: Error) => setError(err.message));
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
    void api<{ mode: string }>("/api/clarity/v1/auth/mode").then((body) => setPasswordMode(body.mode === "password")).catch(() => undefined);
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
  async function assignRoles(id: string, roles: string[]) {
    setError("");
    try {
      const updated = await api<Row>(`/api/clarity/v1/administration/users/${id}`, { method: "PATCH", body: JSON.stringify({ roles }) });
      setRows((current) => current.map((row) => (row.id === id ? updated : row)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Attribution refusée");
    }
  }
  async function addMember(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const created = await api<Row>("/api/clarity/v1/administration/users", {
        method: "POST",
        body: JSON.stringify({ given_name: givenName, family_name: familyName, email, roles: chosen, password: passwordMode ? memberPassword : undefined }),
      });
      setRows((current) => [...current, created]);
      setGivenName("");
      setFamilyName("");
      setEmail("");
      setMemberPassword("");
      setChosen(["analyst"]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ajout refusé");
    }
  }
  const [reseedNote, setReseedNote] = useState("");
  const [reseeding, setReseeding] = useState(false);
  async function purgeAndReseed() {
    setError("");
    setReseedNote("");
    setReseeding(true);
    try {
      const body = await api<Record<string, number>>("/api/clarity/v1/administration/reseed-clients", { method: "POST", body: "{}" });
      setReseedNote(`Clients et vérifications remplacés : ${body.seeded_customers || 0} dossiers tunisiens.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rechargement refusé");
    } finally {
      setReseeding(false);
    }
  }
  return (
    <Board title="Administration" lede={me ? `Session ${String(me.name)}.` : "Équipe et configuration."}>
      <div className="tabs">
        <button className={tab === "equipe" ? "active" : ""} type="button" onClick={() => setTab("equipe")}>Équipe</button>
        <button className={tab === "config" ? "active" : ""} type="button" onClick={() => setTab("config")}>Configuration globale</button>
        <button className={tab === "stockage" ? "active" : ""} type="button" onClick={() => setTab("stockage")}>Stockage</button>
        <button className={tab === "donnees" ? "active" : ""} type="button" onClick={() => setTab("donnees")}>Données démo</button>
      </div>
      {error ? <p className="error">{error}</p> : null}
      {tab === "donnees" ? (
        <article className="card">
          <h3>Remplacer clients et vérifications</h3>
          <p className="label">Supprime les dossiers actuels et charge 100 clients tunisiens avec des scénarios de score différents.</p>
          {reseedNote ? <p className="label">{reseedNote}</p> : null}
          <button type="button" disabled={reseeding} onClick={() => void purgeAndReseed()}>
            {reseeding ? "Rechargement…" : "Purger et recharger 100 cas tunisiens"}
          </button>
        </article>
      ) : null}
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
              <p className="label">Même paramétrage que Ballerine : bucket, clé visible, secret, endpoint MinIO et path-style.</p>
            </>
          ) : (
            <p className="label">Les fichiers restent sur le disque de l&apos;API tant qu&apos;aucun S3 ou MinIO n&apos;est enregistré.</p>
          )}
          <button type="submit">Enregistrer</button>
        </form>
      ) : null}
      {tab === "config" ? (
        <div className="action-grid">
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
          <form className="form card" onSubmit={(event) => void saveMakerChecker(event)}>
            <h3>Séparation maker / checker</h3>
            {makerSaved ? <p className="label">{makerSaved}</p> : null}
            <label className="check">
              <input
                type="checkbox"
                checked={makerChecker}
                onChange={(event) => setMakerChecker(event.target.checked)}
              />
              Empêcher la personne qui a instruit le dossier de l&apos;approuver
            </label>
            <p className="label">Activé : l&apos;analyste qui transmet ne peut pas valider en contrôle. Désactivé : la même personne peut instruire puis approuver (utile en démo).</p>
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
      {tab === "equipe" ? (
        <>
          <form className="form" onSubmit={(event) => void addMember(event)}>
            <label>Prénom<input value={givenName} onChange={(event) => setGivenName(event.target.value)} required /></label>
            <label>Nom<input value={familyName} onChange={(event) => setFamilyName(event.target.value)} required /></label>
            <label>E-mail<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
            {passwordMode ? <label>Mot de passe<input type="password" autoComplete="new-password" minLength={8} value={memberPassword} onChange={(event) => setMemberPassword(event.target.value)} required /></label> : null}
            <div className="checks" role="group" aria-label="Rôles du nouveau membre">
              {TEAM_ROLES.map(([value, label]) => (
                <label key={value}>
                  <input
                    type="checkbox"
                    checked={chosen.includes(value)}
                    onChange={() => setChosen((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value])}
                  />
                  {label}
                </label>
              ))}
            </div>
            <p className="label">{passwordMode ? "L'identifiant est l'e-mail. La personne se connecte avec ce mot de passe." : "L'identifiant est l'e-mail. La personne se connecte ensuite avec Keycloak."}</p>
            <button type="submit">Ajouter</button>
          </form>
          <table className="inbox">
            <thead><tr><th>Nom</th><th>E-mail</th><th>Rôles</th></tr></thead>
            <tbody>
              {rows.map((row) => {
                const held = roleList(row);
                return (
                  <tr key={String(row.id)}>
                    <td>{String(row.name)}</td>
                    <td>{String(row.email)}</td>
                    <td>
                      <div className="checks" role="group" aria-label={`Rôles de ${String(row.name)}`}>
                        {TEAM_ROLES.map(([value, label]) => (
                          <label key={value}>
                            <input
                              type="checkbox"
                              checked={held.includes(value)}
                              onChange={() => {
                                const next = held.includes(value) ? held.filter((item) => item !== value) : [...held, value];
                                void assignRoles(String(row.id), next.filter((item) => item !== "pending"));
                              }}
                            />
                            {label}
                          </label>
                        ))}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      ) : null}
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
