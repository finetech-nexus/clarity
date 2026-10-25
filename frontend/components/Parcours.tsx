"use client";

import { FormEvent, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8095";

export default function Parcours() {
  const params = useSearchParams();
  const token = params.get("token") || "";
  const workflowId = params.get("workflowId") || "";
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => {
    if (!token) return;
    void fetch(`${API}/api/v1/collection-flow/context?workflowRuntimeId=${encodeURIComponent(workflowId)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(await response.text());
        return response.json();
      })
      .then(setState)
      .catch((err: Error) => setError(err.message));
  }, [token, workflowId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    const response = await fetch(`${API}/api/v1/collection-flow/sync/context?workflowRuntimeId=${encodeURIComponent(workflowId)}`, {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ context: { legalName: name, address } }),
    });
    if (!response.ok) {
      setError(await response.text());
      return;
    }
    const finished = await fetch(`${API}/api/v1/collection-flow/final-submission?workflowRuntimeId=${encodeURIComponent(workflowId)}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    setDone(finished.ok ? "Informations transmises à l'analyste. Ceci ne constitue pas une décision." : await finished.text());
  }

  return (
    <main className="main">
      <h1>Parcours client</h1>
      <p className="lede">Lien à usage limité émis pour ce dossier. Le jeton n'ouvre aucun autre client.</p>
      {error ? <p className="error">{error}</p> : null}
      {state ? <p className="label">Étape courante : {String(state.state)} · accès {String(state.auth)}</p> : null}
      <form className="form" onSubmit={save}>
        <div className="step wide">
          <div className="label">Identité</div>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Nom" />
        </div>
        <div className="step wide">
          <div className="label">Adresse</div>
          <input value={address} onChange={(event) => setAddress(event.target.value)} placeholder="Adresse" />
        </div>
        <button type="submit">Transmettre</button>
      </form>
      {done ? <p>{done}</p> : null}
    </main>
  );
}
