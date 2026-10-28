"use client";

import { FormEvent, useEffect, useState } from "react";
import DocumentCatalog from "./DocumentCatalog";
import ScenarioEditor from "./ScenarioEditor";
import WorkflowEditor from "./WorkflowEditor";
import { api } from "../lib/api";

const TABS = ["Scénarios", "Modèle", "Import", "Workflows", "Pièces", "IA", "Listes"] as const;

const COLUMN_TYPES = ["String", "Int", "Float", "Bool", "Timestamp"] as const;
const LOCKED_COLUMNS = new Set(["object_id", "updated_at"]);

type DataField = { id: string; name: string; data_type: string; nullable: boolean; description?: string };
type DataTable = { id: string; name: string; description?: string; fields: Record<string, DataField> };

function tableFields(item: DataTable): DataField[] {
  return Object.values(item.fields || {}).sort((left, right) => left.name.localeCompare(right.name));
}

export default function StudioDesk() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Scénarios");
  const [model, setModel] = useState<Record<string, unknown> | null>(null);
  const [ai, setAi] = useState<Record<string, unknown> | null>(null);
  const [lists, setLists] = useState<Array<Record<string, unknown>>>([]);
  const [providers, setProviders] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [table, setTable] = useState("cards");
  const [openTableId, setOpenTableId] = useState("");
  const [columnName, setColumnName] = useState("");
  const [columnType, setColumnType] = useState<(typeof COLUMN_TYPES)[number]>("String");
  const [columnNullable, setColumnNullable] = useState(true);
  const [drafts, setDrafts] = useState<DataField[]>([]);
  const [payload, setPayload] = useState('{"object_id":"tx-import-1","updated_at":"2026-10-25T00:00:00Z","amount":1200,"currency":"EUR","direction":"outbound","country":"FR"}');

  useEffect(() => {
    void Promise.all([
      api<Record<string, unknown>>("/data-model"),
      api<Record<string, unknown>>("/settings/ai"),
      api<Array<Record<string, unknown>>>("/custom-lists"),
      api<Record<string, unknown>>("/config"),
    ]).then(([dataModel, aiSettings, listRows, config]) => {
      setModel(dataModel);
      setAi(aiSettings);
      setLists(listRows);
      setProviders(config);
    }).catch((err: Error) => setMessage(err.message));
  }, []);

  async function reloadModel(nextId?: string) {
    const dataModel = await api<Record<string, unknown>>("/data-model");
    setModel(dataModel);
    const tables = ((dataModel.tables || {}) as Record<string, DataTable>);
    const opened = Object.values(tables).find((item) => item.id === (nextId || openTableId));
    setDrafts(opened ? tableFields(opened) : []);
    return dataModel;
  }

  async function createTable(event: FormEvent) {
    event.preventDefault();
    try {
      const created = await api<DataTable>("/data-model/tables", { method: "POST", body: JSON.stringify({ name: table, semantic_type: "transactions", description: "" }) });
      setOpenTableId(created.id);
      await reloadModel(created.id);
      setMessage(`Table ${created.name} créée. object_id et updated_at sont exigés par l'ingestion.`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "La table n'a pas été créée.");
    }
  }

  function chooseTable(item: DataTable) {
    setOpenTableId(item.id);
    setDrafts(tableFields(item));
    setColumnName("");
    setMessage("");
  }

  async function addColumn(event: FormEvent) {
    event.preventDefault();
    if (!openTableId) return;
    try {
      await api(`/data-model/tables/${openTableId}/fields`, {
        method: "POST",
        body: JSON.stringify({ name: columnName.trim(), data_type: columnType, nullable: columnNullable }),
      });
      setColumnName("");
      await reloadModel(openTableId);
      setMessage("Colonne ajoutée.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "La colonne n'a pas été ajoutée.");
    }
  }

  async function saveColumn(field: DataField) {
    try {
      await api(`/data-model/fields/${field.id}`, {
        method: "PATCH",
        body: JSON.stringify({ name: field.name.trim(), data_type: field.data_type, nullable: field.nullable }),
      });
      await reloadModel(openTableId);
      setMessage(`Colonne ${field.name} enregistrée.`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "La colonne n'a pas été enregistrée.");
    }
  }

  function updateDraft(id: string, patch: Partial<DataField>) {
    setDrafts((current) => current.map((field) => (field.id === id ? { ...field, ...patch } : field)));
  }

  async function importObject(event: FormEvent) {
    event.preventDefault();
    const body = JSON.parse(payload);
    const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8095"}/ingestion/transactions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${sessionStorage.getItem("clarity_token") || ""}` },
      body: JSON.stringify(body),
    });
    setMessage(response.ok || response.status === 201 ? `Import accepté (${response.status}).` : await response.text());
  }

  async function saveAi(enabled: boolean) {
    const saved = await api<Record<string, unknown>>("/settings/ai", {
      method: "PUT",
      body: JSON.stringify({ kyc_enrichment: { enabled, domain_filter: ["societe.com"], model: null, custom_instructions: "Ne confirme aucune correspondance." } }),
    });
    setAi(saved);
  }

  const tables = ((model || {}) as { tables?: Record<string, DataTable> }).tables || {};
  const tableList = Object.values(tables).sort((left, right) => left.name.localeCompare(right.name));
  const openTable = tableList.find((item) => item.id === openTableId);
  const provider = (providers?.providers || {}) as Record<string, boolean>;

  return (
    <>
      <h1>Paramétrage</h1>
      <p className="lede">Atelier unique pour le modèle, les scénarios, les workflows et l'assistance d'analyse. OpenSanctions, Veriff et le modèle de langage ne répondent que si leurs clés sont configurées.</p>
      <div className="tabs">
        {TABS.map((item) => (
          <button className={item === tab ? "active" : ""} key={item} onClick={() => setTab(item)} type="button">{item}</button>
        ))}
      </div>
      {message ? <p>{message}</p> : null}
      {tab === "Scénarios" ? <ScenarioEditor /> : null}
      {tab === "Modèle" ? (
        openTable ? (
          <section className="case-sheet">
            <header className="case-bar">
              <div>
                <button className="linkish" type="button" onClick={() => { setOpenTableId(""); setDrafts([]); }}>Toutes les tables</button>
                <h2>{openTable.name}</h2>
                <p className="label">{openTable.description || "Colonnes de la table. object_id et updated_at restent exigés par l'ingestion."}</p>
              </div>
            </header>
            <form className="form" onSubmit={(event) => void addColumn(event)}>
              <input aria-label="Nom de la colonne" placeholder="Nom de la colonne" value={columnName} onChange={(event) => setColumnName(event.target.value)} required />
              <select aria-label="Type de la colonne" value={columnType} onChange={(event) => setColumnType(event.target.value as (typeof COLUMN_TYPES)[number])}>
                {COLUMN_TYPES.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <label className="check"><input type="checkbox" checked={columnNullable} onChange={(event) => setColumnNullable(event.target.checked)} /> Nullable</label>
              <button type="submit">Ajouter la colonne</button>
            </form>
            <table className="inbox">
              <thead>
                <tr>
                  <th>Colonne</th>
                  <th>Type</th>
                  <th>Nullable</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {drafts.map((field) => {
                  const locked = LOCKED_COLUMNS.has(field.name);
                  return (
                    <tr key={field.id}>
                      <td><input aria-label={`Nom ${field.name}`} value={field.name} disabled={locked} onChange={(event) => updateDraft(field.id, { name: event.target.value })} /></td>
                      <td>
                        <select aria-label={`Type ${field.name}`} value={field.data_type} onChange={(event) => updateDraft(field.id, { data_type: event.target.value })}>
                          {COLUMN_TYPES.map((item) => <option key={item} value={item}>{item}</option>)}
                          {(COLUMN_TYPES as readonly string[]).includes(field.data_type) ? null : <option value={field.data_type}>{field.data_type}</option>}
                        </select>
                      </td>
                      <td><label className="check"><input type="checkbox" checked={field.nullable} disabled={locked} onChange={(event) => updateDraft(field.id, { nullable: event.target.checked })} /> Nullable</label></td>
                      <td><button type="button" onClick={() => void saveColumn(field)}>Enregistrer</button></td>
                    </tr>
                  );
                })}
                {!drafts.length ? <tr><td colSpan={4}>Aucune colonne.</td></tr> : null}
              </tbody>
            </table>
          </section>
        ) : (
          <>
            <form className="form" onSubmit={(event) => void createTable(event)}>
              <input aria-label="Nom de la table" value={table} onChange={(event) => setTable(event.target.value)} />
              <button type="submit">Ajouter la table</button>
            </form>
            <div className="queue">
              {tableList.map((item) => (
                <button className="ghost" key={item.id || item.name} type="button" onClick={() => chooseTable(item)}>
                  <span>{item.name}</span>
                  <span className="label">{tableFields(item).length} colonnes</span>
                </button>
              ))}
            </div>
            {!tableList.length ? <p className="label">Aucune table dans le modèle.</p> : null}
          </>
        )
      ) : null}
      {tab === "Import" ? (
        <form className="form" onSubmit={importObject}>
          <textarea className="wide" value={payload} onChange={(event) => setPayload(event.target.value)} />
          <button type="submit">Importer la transaction</button>
        </form>
      ) : null}
      {tab === "Workflows" ? <WorkflowEditor embedded /> : null}
      {tab === "Pièces" ? <DocumentCatalog /> : null}
      {tab === "IA" ? (
        <article className="card">
          <div className="label">Fournisseurs</div>
          <p>OpenSanctions : {provider.opensanctions ? "clé présente" : "non configuré, jeu synthétique"}</p>
          <p>Veriff : {provider.veriff ? "clé présente" : "non configuré, aucune session n'est inventée"}</p>
          <p>Modèle : {provider.ai ? "clé présente" : "analyste local, marqué hors production"}</p>
          <p>Enrichissement KYC : {String((ai?.kyc_enrichment as { enabled?: boolean } | undefined)?.enabled ? "activé" : "désactivé")}</p>
          <button type="button" onClick={() => void saveAi(true)}>Activer l'enrichissement</button>
        </article>
      ) : null}
      {tab === "Listes" ? (
        <div className="list">{lists.map((row) => <div className="row" key={String(row.id)}><span>{String(row.name)}<div className="label">{String(row.description || "")}</div></span></div>)}</div>
      ) : null}
    </>
  );
}
