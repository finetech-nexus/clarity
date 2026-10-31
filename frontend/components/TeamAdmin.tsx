"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";

type Member = {
  id: string;
  email: string;
  name: string;
  role?: string;
  roles?: string[];
};

const TEAM_ROLES = [
  ["analyst", "Analyste"],
  ["checker", "Checker"],
  ["investigator", "Investigateur"],
  ["admin", "Administrateur"],
] as const;

function roleList(row: Member): string[] {
  if (Array.isArray(row.roles)) return row.roles.map(String);
  return String(row.role || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function roleLabels(row: Member): string {
  const held = roleList(row);
  const labels = TEAM_ROLES.filter(([value]) => held.includes(value)).map(([, label]) => label);
  return labels.join(", ") || "—";
}

function splitName(name: string): { given: string; family: string } {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length <= 1) return { given: parts[0] || "", family: "" };
  return { given: parts[0], family: parts.slice(1).join(" ") };
}

const EMPTY = { id: "", given: "", family: "", email: "", password: "", roles: ["analyst"] };

export function TeamAdmin() {
  const [rows, setRows] = useState<Member[]>([]);
  const [query, setQuery] = useState("");
  const [screen, setScreen] = useState<"list" | "form">("list");
  const [draft, setDraft] = useState(EMPTY);
  const [passwordMode, setPasswordMode] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    void api<Member[]>("/api/clarity/v1/administration/users").then(setRows).catch((err: Error) => setError(err.message));
    void api<{ mode: string }>("/api/clarity/v1/auth/mode").then((body) => setPasswordMode(body.mode === "password")).catch(() => undefined);
  }, []);

  const needle = query.trim().toLowerCase();
  const visible = rows.filter((row) => {
    if (!needle) return true;
    return row.name.toLowerCase().includes(needle) || row.email.toLowerCase().includes(needle);
  });

  function openCreate() {
    setError("");
    setDraft(EMPTY);
    setScreen("form");
  }

  function openEdit(row: Member) {
    const name = splitName(row.name);
    setError("");
    setDraft({
      id: row.id,
      given: name.given,
      family: name.family,
      email: row.email,
      password: "",
      roles: roleList(row).filter((item) => item !== "pending"),
    });
    setScreen("form");
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    const roles = draft.roles.filter((item) => item !== "pending");
    try {
      if (draft.id) {
        const updated = await api<Member>(`/api/clarity/v1/administration/users/${draft.id}`, {
          method: "PATCH",
          body: JSON.stringify({
            given_name: draft.given,
            family_name: draft.family,
            email: draft.email,
            roles,
            password: passwordMode && draft.password ? draft.password : undefined,
          }),
        });
        setRows((current) => current.map((row) => (row.id === updated.id ? updated : row)));
      } else {
        const created = await api<Member>("/api/clarity/v1/administration/users", {
          method: "POST",
          body: JSON.stringify({
            given_name: draft.given,
            family_name: draft.family,
            email: draft.email,
            roles,
            password: passwordMode ? draft.password : undefined,
          }),
        });
        setRows((current) => [...current, created]);
      }
      setDraft(EMPTY);
      setScreen("list");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  if (screen === "form") {
    return (
      <form className="form card" onSubmit={(event) => void save(event)}>
        <div className="actions wide">
          <h3>{draft.id ? "Modifier le membre" : "Nouveau membre"}</h3>
          <button type="button" className="ghost" onClick={() => { setError(""); setScreen("list"); }}>Retour</button>
        </div>
        {error ? <p className="error wide">{error}</p> : null}
        <label>Prénom<input value={draft.given} onChange={(event) => setDraft((current) => ({ ...current, given: event.target.value }))} required /></label>
        <label>Nom<input value={draft.family} onChange={(event) => setDraft((current) => ({ ...current, family: event.target.value }))} required /></label>
        <label>E-mail<input type="email" value={draft.email} onChange={(event) => setDraft((current) => ({ ...current, email: event.target.value }))} required /></label>
        {passwordMode ? (
          <label>
            Mot de passe
            <input
              type="password"
              autoComplete="new-password"
              minLength={draft.id ? undefined : 8}
              value={draft.password}
              placeholder={draft.id ? "Laisser vide pour conserver" : ""}
              onChange={(event) => setDraft((current) => ({ ...current, password: event.target.value }))}
              required={!draft.id}
            />
          </label>
        ) : null}
        <div className="checks wide" role="group" aria-label="Rôles">
          {TEAM_ROLES.map(([value, label]) => (
            <label key={value}>
              <input
                type="checkbox"
                checked={draft.roles.includes(value)}
                onChange={() => {
                  setDraft((current) => ({
                    ...current,
                    roles: current.roles.includes(value) ? current.roles.filter((item) => item !== value) : [...current.roles, value],
                  }));
                }}
              />
              {label}
            </label>
          ))}
        </div>
        <p className="label wide">{passwordMode ? "L'identifiant est l'e-mail. La personne se connecte avec ce mot de passe." : "L'identifiant est l'e-mail. La personne se connecte ensuite avec Keycloak."}</p>
        <button type="submit">{draft.id ? "Enregistrer" : "Créer"}</button>
      </form>
    );
  }

  return (
    <div className="stack">
      {error ? <p className="error">{error}</p> : null}
      <div className="actions">
        <input aria-label="Recherche" placeholder="Nom ou e-mail" value={query} onChange={(event) => setQuery(event.target.value)} />
        <button type="button" onClick={openCreate}>Nouveau</button>
      </div>
      <table className="inbox">
        <thead><tr><th>Nom</th><th>E-mail</th><th>Rôles</th><th></th></tr></thead>
        <tbody>
          {!visible.length ? <tr><td colSpan={4}>Aucun membre.</td></tr> : null}
          {visible.map((row) => (
            <tr key={row.id}>
              <td>{row.name}</td>
              <td>{row.email}</td>
              <td>{roleLabels(row)}</td>
              <td><button type="button" onClick={() => openEdit(row)}>Modifier</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
