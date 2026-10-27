"use client";

import { useEffect, useState } from "react";
import { API_URL, authHeader } from "../lib/api";

export type ViewerDocument = {
  id: string;
  title: string;
  fileName: string;
  decision?: string | null;
  status?: string;
  url: string;
  mime?: string;
  synthetic?: boolean;
};

const DECISION_LABEL: Record<string, string> = {
  approved: "Approuvé",
  rejected: "Rejeté",
  revisions: "À renvoyer",
};

function DocumentCard({
  document,
  onDecide,
}: {
  document: ViewerDocument;
  onDecide?: (id: string, decision: "approve" | "reject" | "revision") => void;
}) {
  const [src, setSrc] = useState("");
  const [rotation, setRotation] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState("");

  useEffect(() => {
    let objectUrl = "";
    let cancelled = false;
    setSrc("");
    setError("");
    void fetch(`${API_URL}${document.url}`, { headers: authHeader() })
      .then(async (response) => {
        if (!response.ok) throw new Error(await response.text());
        return response.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [document.id, document.url]);

  return (
    <article className="viewer">
      <div className="viewer-caption">
        <strong>{document.title}</strong>
        <span className="label">{document.fileName} · {DECISION_LABEL[document.decision || ""] || document.status || "fourni"}</span>
      </div>
      <div className="viewer-stage">
        <div className="viewer-canvas">
          {src ? (
            <img alt={document.title} src={src} style={{ transform: `rotate(${rotation}deg) scale(${zoom})` }} />
          ) : (
            <p className="label">{error || "Chargement de la pièce…"}</p>
          )}
        </div>
        <div className="viewer-tools">
          <button className="ghost" type="button" onClick={() => setRotation((value) => value - 90)} aria-label="Pivoter à gauche">↺</button>
          <button className="ghost" type="button" onClick={() => setRotation((value) => value + 90)} aria-label="Pivoter à droite">↻</button>
          <button className="ghost" type="button" onClick={() => setZoom((value) => Math.min(2.4, value + 0.2))} aria-label="Agrandir">+</button>
          <button className="ghost" type="button" onClick={() => setZoom((value) => Math.max(0.6, value - 0.2))} aria-label="Réduire">−</button>
          {src ? <a className="ghost tool-link" href={src} target="_blank" rel="noreferrer">Ouvrir</a> : null}
        </div>
      </div>
      {onDecide ? (
        <div className="tabs">
          <button type="button" onClick={() => onDecide(document.id, "approve")}>Approuver la pièce</button>
          <button className="ghost" type="button" onClick={() => onDecide(document.id, "revision")}>À renvoyer</button>
          <button className="ghost" type="button" onClick={() => onDecide(document.id, "reject")}>Rejeter la pièce</button>
        </div>
      ) : null}
    </article>
  );
}

export default function DocumentViewer({
  documents,
  onDecide,
}: {
  documents: ViewerDocument[];
  onDecide?: (id: string, decision: "approve" | "reject" | "revision") => void;
}) {
  if (!documents.length) {
    return <p className="label">Aucun document fourni pour ce dossier.</p>;
  }
  return (
    <div className="doc-stack">
      {documents.map((document) => (
        <DocumentCard document={document} key={document.id} onDecide={onDecide} />
      ))}
    </div>
  );
}
