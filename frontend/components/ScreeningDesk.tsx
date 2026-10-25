"use client";

import { useEffect, useState } from "react";
import { QueryStack } from "./QueryStack";
import { api, searchPage } from "../lib/api";

type Match = {
  id: string;
  status: string;
  score: number;
  entity_id: string;
  name?: string;
  topics?: string[];
  datasets?: string[];
  explanation?: string[];
  note?: string;
  synthetic?: boolean;
  provider?: string;
};

export default function ScreeningDesk() {
  const [rows, setRows] = useState<Match[]>([]);
  const [selected, setSelected] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("status:pending");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);

  async function load(prefer?: string, nextQuery = query, nextPage = page) {
    const body = await searchPage<Match>("/api/clarity/v1/screening/matches", nextQuery, nextPage);
    setRows(body.items);
    setTotal(body.total);
    setSelected(prefer && body.items.some((row) => row.id === prefer) ? prefer : body.items[0]?.id || "");
  }

  useEffect(() => {
    void load(undefined, query, page).catch((err: Error) => setError(err.message));
  }, [query, page]);

  const current = rows.find((row) => row.id === selected) || rows[0];

  async function decide(status: "no_hit" | "confirmed_hit") {
    if (!current) return;
    setError("");
    try {
      await api(`/api/clarity/v1/screening/matches/${current.id}/disposition`, {
        method: "POST",
        body: JSON.stringify({ status, rationale: "Qualification manuelle dans Clarity." }),
      });
      setMessage(status === "no_hit" ? "Marquée faux positif. Ce n'est pas un rejet automatique du client." : "Correspondance confirmée par un analyste. Le score seul ne l'avait pas confirmée.");
      await load(current.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Qualification refusée");
    }
  }

  return (
    <>
      <h1>Criblage</h1>
      <p className="lede">Chaque ligne est une correspondance candidate. Le score n'approuve ni ne rejette personne.</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <QueryStack
        query={query}
        onChange={(next) => { setPage(0); setQuery(next); }}
        fields={[{ key: "status", label: "Statut", options: ["pending", "no_hit", "confirmed_hit"] }, { key: "name", label: "Nom" }, { key: "provider", label: "Source" }]}
        total={total}
        page={page}
        pageSize={25}
        onPage={setPage}
      />
      <div className="screen-shell">
        <div className="queue">
          {rows.map((row) => (
            <button className={row.id === current?.id ? "active" : "ghost"} key={row.id} type="button" onClick={() => setSelected(row.id)}>
              <span>{row.name || row.entity_id}</span>
              <span className="label">{row.status} · {row.score}</span>
              <span className="meter"><span style={{ width: `${Math.max(0, Math.min(100, row.score))}%` }} /></span>
            </button>
          ))}
          {!rows.length ? <p className="label">Aucune correspondance dans ce filtre.</p> : null}
        </div>
        {current ? (
          <article className="card">
            <header className="case-bar">
              <div>
                <h2>{current.name || current.entity_id}</h2>
                <p className="label">{current.entity_id} · {current.provider || "liste"} · {current.synthetic ? "jeu synthétique" : "source externe"}</p>
              </div>
              <span className={`badge ${current.status}`}>{current.score}</span>
            </header>
            <div className="meter large"><span style={{ width: `${Math.max(0, Math.min(100, current.score))}%` }} /></div>
            <dl className="kv">
              <dt>Statut</dt><dd>{current.status}</dd>
              <dt>Thèmes</dt><dd>{(current.topics || []).join(", ") || "—"}</dd>
              <dt>Jeux</dt><dd>{(current.datasets || []).join(", ") || "—"}</dd>
            </dl>
            {(current.explanation || []).map((line) => <p className="factor" key={line}>{line}</p>)}
            {current.note ? <p className="label">{current.note}</p> : null}
            {current.status === "pending" ? (
              <div className="tabs">
                <button type="button" onClick={() => void decide("no_hit")}>Faux positif</button>
                <button className="ghost" type="button" onClick={() => void decide("confirmed_hit")}>Confirmer</button>
              </div>
            ) : null}
          </article>
        ) : null}
      </div>
    </>
  );
}
