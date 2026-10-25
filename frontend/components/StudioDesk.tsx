"use client";

import { FormEvent, useEffect, useState } from "react";
import DocumentCatalog from "./DocumentCatalog";
import ScenarioEditor from "./ScenarioEditor";
import WorkflowEditor from "./WorkflowEditor";
import { api } from "../lib/api";

const TABS = ["Scénarios", "Modèle", "Import", "Workflows", "Pièces", "IA", "Listes"] as const;

export default function StudioDesk() {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Scénarios");
  const [model, setModel] = useState<Record<string, unknown> | null>(null);
  const [ai, setAi] = useState<Record<string, unknown> | null>(null);
  const [lists, setLists] = useState<Array<Record<string, unknown>>>([]);
  const [providers, setProviders] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [table, setTable] = useState("cards");
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

  async function createTable(event: FormEvent) {
    event.preventDefault();
    await api("/data-model/tables", { method: "POST", body: JSON.stringify({ name: table, semantic_type: "transactions", description: "Table créée dans l'atelier" }) });
    setModel(await api("/data-model"));
    setMessage(`Table ${table} ajoutée au modèle. object_id et updated_at sont exigés par l'ingestion.`);
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

  const tables = Object.keys(((model || {}) as { tables?: Record<string, unknown> }).tables || {});
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
        <>
          <form className="form" onSubmit={createTable}>
            <input value={table} onChange={(event) => setTable(event.target.value)} />
            <button type="submit">Ajouter la table</button>
          </form>
          <div className="list">{tables.map((item) => <div className="row" key={item}><span>{item}</span></div>)}</div>
        </>
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
