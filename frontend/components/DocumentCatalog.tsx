"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";

type Definition = { id: string; name: string; displayName?: string; entityType?: string; documentsSchema?: DocumentType[] };
type DocumentType = {
  id: string;
  category: string;
  type: string;
  issuer: { country: string };
  version: number;
  required: boolean;
  propertiesSchema?: { type: string; properties: Record<string, unknown> };
};

export default function DocumentCatalog() {
  const [definitions, setDefinitions] = useState<Definition[]>([]);
  const [selected, setSelected] = useState("");
  const [rows, setRows] = useState<DocumentType[]>([]);
  const [category, setCategory] = useState("identity");
  const [type, setType] = useState("residence_permit");
  const [country, setCountry] = useState("FR");
  const [required, setRequired] = useState(true);
  const [message, setMessage] = useState("");

  async function load(prefer?: string) {
    const list = await api<Definition[]>("/api/v1/workflow-definition");
    setDefinitions(list);
    const id = prefer && list.some((row) => row.id === prefer) ? prefer : list[0]?.id || "";
    setSelected(id);
    setRows(list.find((row) => row.id === id)?.documentsSchema || []);
  }

  useEffect(() => {
    void load().catch((err: Error) => setMessage(err.message));
  }, []);

  function choose(id: string) {
    setSelected(id);
    setRows(definitions.find((row) => row.id === id)?.documentsSchema || []);
  }

  async function save(next: DocumentType[]) {
    const saved = await api<Definition>(`/api/v1/workflow-definition/${selected}/documents-schema`, {
      method: "PUT",
      body: JSON.stringify(next),
    });
    setRows(saved.documentsSchema || []);
    setDefinitions((current) => current.map((row) => (row.id === selected ? saved : row)));
    setMessage("Liste de pièces enregistrée sur le parcours. Les dossiers ouverts l'affichent dans le suivi.");
  }

  async function addType(event: FormEvent) {
    event.preventDefault();
    await save([
      ...rows,
      { id: `doc-${Date.now()}`, category, type, issuer: { country }, version: 1, required, propertiesSchema: { type: "object", properties: {} } },
    ]);
  }

  const current = definitions.find((row) => row.id === selected);

  return (
    <div className="editor">
      <div className="queue">
        {definitions.map((row) => (
          <button className={row.id === selected ? "active" : "ghost"} key={row.id} type="button" onClick={() => choose(row.id)}>
            <span>{row.displayName || row.name}</span>
            <span className="label">{row.entityType} · {(row.documentsSchema || []).length} type(s)</span>
          </button>
        ))}
      </div>
      <section className="case-sheet">
        <header className="case-bar">
          <div>
            <h2>Types de documents</h2>
            <p className="label">{current?.displayName || "Parcours"} · catégorie, type et pays émetteur, comme le schéma de pièces d'un parcours.</p>
          </div>
        </header>
        {message ? <p className="label">{message}</p> : null}
        <form className="form" onSubmit={addType}>
          <input aria-label="Catégorie" value={category} onChange={(event) => setCategory(event.target.value)} />
          <input aria-label="Type" value={type} onChange={(event) => setType(event.target.value)} />
          <input aria-label="Pays" value={country} onChange={(event) => setCountry(event.target.value)} />
          <label className="check"><input type="checkbox" checked={required} onChange={(event) => setRequired(event.target.checked)} /> Obligatoire</label>
          <button type="submit">Ajouter le type</button>
        </form>
        <div className="list">
          {rows.map((row) => (
            <div className="row" key={row.id}>
              <span>{row.type}<div className="label">{row.category} · {row.issuer.country} · {row.required ? "obligatoire" : "facultatif"}</div></span>
              <button className="ghost" type="button" onClick={() => void save(rows.filter((item) => item.id !== row.id))}>Retirer</button>
            </div>
          ))}
          {!rows.length ? <p className="label">Aucun type configuré sur ce parcours.</p> : null}
        </div>
      </section>
    </div>
  );
}
