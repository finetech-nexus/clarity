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

export default function DocumentViewer({
  documents,
  onDecide,
}: {
  documents: ViewerDocument[];
  onDecide?: (id: string, decision: "approve" | "reject" | "revision") => void;
}) {
  const [selected, setSelected] = useState(documents[0]?.id || "");
  const [src, setSrc] = useState("");
  const [rotation, setRotation] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState("");
  const current = documents.find((item) => item.id === selected) || documents[0];

  useEffect(() => {
    if (!documents.some((item) => item.id === selected)) setSelected(documents[0]?.id || "");
  }, [documents, selected]);

  useEffect(() => {
    if (!current) return;
    let objectUrl = "";
    let cancelled = false;
    setSrc("");
    setRotation(0);
    setZoom(1);
    setError("");
    void fetch(`${API_URL}${current.url}`, { headers: authHeader() })
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
  }, [current?.id]);

  if (!current) {
    return <p className="label">Aucun document fourni pour ce dossier.</p>;
  }

  return (
    <section className="viewer">
      <div className="viewer-stage">
        <div className="viewer-canvas">
          {src ? (
            <img
              alt={current.title}
              src={src}
              style={{ transform: `rotate(${rotation}deg) scale(${zoom})` }}
            />
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
      <div className="viewer-caption">
        <strong>{current.title}</strong>
        <span className="label">{current.fileName}{current.synthetic ? " · specimen" : ""}{current.decision ? ` · ${DECISION_LABEL[current.decision] || current.decision}` : ""}</span>
      </div>
      {onDecide ? (
        <div className="tabs">
          <button type="button" onClick={() => onDecide(current.id, "approve")}>Approuver la pièce</button>
          <button className="ghost" type="button" onClick={() => onDecide(current.id, "revision")}>À renvoyer</button>
          <button className="ghost" type="button" onClick={() => onDecide(current.id, "reject")}>Rejeter la pièce</button>
        </div>
      ) : null}
      <div className="thumbs">
        {documents.map((item) => (
          <button className={item.id === current.id ? "active" : "ghost"} key={item.id} type="button" onClick={() => setSelected(item.id)}>
            <span>{item.title}</span>
            <span className="label">{DECISION_LABEL[item.decision || ""] || item.status || "fourni"}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
