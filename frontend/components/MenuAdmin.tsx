"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";

type WorkflowDefinition = {
  id: string;
  name?: string;
  displayName?: string;
  processKey?: string | null;
};

export type DeskMenu = {
  id: string;
  technical_id: string;
  category: "kyc" | "kyb" | "aml";
  label_fr: string;
  label_en: string;
  workflows: WorkflowDefinition[];
  process_keys: string[];
};

const EMPTY = {
  id: "",
  technical_id: "",
  category: "kyc" as DeskMenu["category"],
  label_fr: "",
  label_en: "",
  workflows: [] as string[],
};

export function MenuAdmin() {
  const [rows, setRows] = useState<DeskMenu[]>([]);
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([]);
  const [draft, setDraft] = useState(EMPTY);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  async function reload() {
    setRows(await api<DeskMenu[]>("/api/clarity/v1/menus"));
  }

  useEffect(() => {
    void reload().catch((err: Error) => setError(err.message));
    void api<WorkflowDefinition[]>("/api/v1/workflow-definition")
      .then(setDefinitions)
      .catch(() => setDefinitions([]));
  }, []);

  function edit(row: DeskMenu) {
    setSaved("");
    setError("");
    setDraft({
      id: row.id,
      technical_id: row.technical_id,
      category: row.category,
      label_fr: row.label_fr,
      label_en: row.label_en,
      workflows: row.workflows.map((item) => item.id),
    });
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    setSaved("");
    const body = {
      technical_id: draft.technical_id,
      category: draft.category,
      label_fr: draft.label_fr,
      label_en: draft.label_en,
      workflows: draft.workflows,
    };
    try {
      if (draft.id) {
        await api(`/api/clarity/v1/administration/menus/${draft.id}`, { method: "PATCH", body: JSON.stringify(body) });
      } else {
        await api("/api/clarity/v1/administration/menus", { method: "POST", body: JSON.stringify(body) });
      }
      setDraft(EMPTY);
      setSaved("Menu enregistré.");
      await reload();
      window.dispatchEvent(new Event("clarity-menus-changed"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function remove(row: DeskMenu) {
    setError("");
    try {
      await api(`/api/clarity/v1/administration/menus/${row.id}`, { method: "DELETE" });
      if (draft.id === row.id) setDraft(EMPTY);
      await reload();
      window.dispatchEvent(new Event("clarity-menus-changed"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Suppression refusée");
    }
  }

  return (
    <div className="stack">
      {error ? <p className="error">{error}</p> : null}
      <article className="card">
        <h3>Menus</h3>
        <table>
          <thead>
            <tr>
              <th>Catégorie</th>
              <th>Libellé</th>
              <th>Identifiant</th>
              <th>Workflows</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.category.toUpperCase()}</td>
                <td>{row.label_fr} / {row.label_en}</td>
                <td>{row.technical_id}</td>
                <td>{row.workflows.map((item) => item.displayName || item.name).join(", ") || "—"}</td>
                <td>
                  <button type="button" onClick={() => edit(row)}>Modifier</button>
                  <button type="button" className="ghost" onClick={() => void remove(row)}>Supprimer</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </article>
      <form className="form card" onSubmit={(event) => void save(event)}>
        <h3>{draft.id ? "Modifier le menu" : "Nouveau menu"}</h3>
        {saved ? <p className="label">{saved}</p> : null}
        <label>
          Catégorie
          <select value={draft.category} onChange={(event) => setDraft((current) => ({ ...current, category: event.target.value as DeskMenu["category"] }))}>
            <option value="kyc">KYC</option>
            <option value="kyb">KYB</option>
            <option value="aml">AML</option>
          </select>
        </label>
        <label>
          Libellé français
          <input value={draft.label_fr} onChange={(event) => setDraft((current) => ({ ...current, label_fr: event.target.value }))} required />
        </label>
        <label>
          Libellé anglais
          <input value={draft.label_en} onChange={(event) => setDraft((current) => ({ ...current, label_en: event.target.value }))} required />
        </label>
        <label>
          Identifiant technique
          <input value={draft.technical_id} onChange={(event) => setDraft((current) => ({ ...current, technical_id: event.target.value }))} required />
        </label>
        <fieldset className="checks">
          <legend>Workflows</legend>
          {definitions.map((item) => (
            <label key={item.id}>
              <input
                type="checkbox"
                checked={draft.workflows.includes(item.id)}
                onChange={() => {
                  setDraft((current) => ({
                    ...current,
                    workflows: current.workflows.includes(item.id)
                      ? current.workflows.filter((id) => id !== item.id)
                      : [...current.workflows, item.id],
                  }));
                }}
              />
              {item.displayName || item.name}
            </label>
          ))}
        </fieldset>
        <button type="submit">{draft.id ? "Enregistrer" : "Créer le menu"}</button>
        {draft.id ? <button type="button" className="ghost" onClick={() => setDraft(EMPTY)}>Annuler</button> : null}
      </form>
    </div>
  );
}
