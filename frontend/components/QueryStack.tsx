"use client";

import { FormEvent, useState } from "react";

export type QueryField = { key: string; label: string; options?: string[] };

export function queryMatches(query: string, fields: Record<string, string>): boolean {
  const filters: Array<[string, string]> = [];
  const words: string[] = [];
  for (const token of query.match(/"[^"]*"|\S+/g) || []) {
    const bare = token.replaceAll('"', "");
    if (!bare) continue;
    if (bare.includes(":") && !token.startsWith('"')) {
      const [field, value] = bare.split(":", 2);
      if (field && value) {
        filters.push([field.toLowerCase(), value.toLowerCase()]);
        continue;
      }
    }
    words.push(bare.toLowerCase());
  }
  const blob = Object.values(fields).join(" ").toLowerCase();
  return filters.every(([field, value]) => (fields[field] || "").toLowerCase().includes(value)) && words.every((word) => blob.includes(word));
}

function chips(query: string) {
  return (query.match(/"[^"]*"|\S+/g) || []).filter((token) => token.includes(":") && !token.startsWith('"'));
}

export function QueryStack({
  query,
  onChange,
  fields,
  total,
  page,
  pageSize,
  onPage,
  compact = false,
}: {
  query: string;
  onChange: (query: string) => void;
  fields: QueryField[];
  total: number;
  page: number;
  pageSize: number;
  onPage: (page: number) => void;
  compact?: boolean;
}) {
  const [field, setField] = useState(fields[0]?.key || "");
  const [value, setValue] = useState(fields[0]?.options?.[0] || "");
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const chosen = fields.find((item) => item.key === field);

  function addFilter(event: FormEvent) {
    event.preventDefault();
    if (!field || !value) return;
    onChange(`${query} ${field}:${value}`.trim());
    setValue(chosen?.options?.[0] || "");
  }

  function removeChip(token: string) {
    onChange(query.replace(token, "").replace(/\s+/g, " ").trim());
  }

  const pager = (
    <div className="chips">
      {chips(query).map((token) => (
        <button className="chip" key={token} type="button" onClick={() => removeChip(token)}>{token} ×</button>
      ))}
      <span className="label">{total} résultat{total > 1 ? "s" : ""} · page {page + 1}/{pages}</span>
      <button className="ghost" type="button" disabled={page <= 0} onClick={() => onPage(page - 1)}>Précédent</button>
      <button className="ghost" type="button" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>Suivant</button>
    </div>
  );

  if (compact) {
    return (
      <div className="query-stack compact">
        <input aria-label="Recherche" value={query} onChange={(event) => onChange(event.target.value)} placeholder="nom, ou process:clarityKycIndividual" />
        {pager}
      </div>
    );
  }

  return (
    <div className="query-stack">
      <label>
        Recherche
        <input value={query} onChange={(event) => onChange(event.target.value)} placeholder="nom, ou statut:ouvert pays:FR" />
      </label>
      <form className="query-add" onSubmit={addFilter}>
        <select value={field} onChange={(event) => { setField(event.target.value); setValue(""); }} aria-label="Champ du filtre">
          {fields.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
        </select>
        {chosen?.options ? (
          <select value={value} onChange={(event) => setValue(event.target.value)} aria-label="Valeur du filtre">
            <option value="">Choisir</option>
            {chosen.options.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        ) : (
          <input value={value} onChange={(event) => setValue(event.target.value)} aria-label="Valeur du filtre" placeholder="valeur" />
        )}
        <button className="ghost" type="submit">Ajouter</button>
      </form>
      {pager}
    </div>
  );
}
