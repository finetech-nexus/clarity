"use client";

import { useEffect, useState } from "react";
import { API_URL, authHeader } from "../lib/api";
import { useI18n } from "../lib/i18n";

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

function DocumentCard({
  document,
  onDecide,
  locked = false,
}: {
  document: ViewerDocument;
  onDecide?: (id: string, decision: "approve" | "reject" | "revision") => void;
  locked?: boolean;
}) {
  const { t } = useI18n();
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
        if (!response.ok) {
          const text = await response.text();
          let message = text || t("verify.docMissing");
          try {
            const body = JSON.parse(text) as { message?: string };
            if (body.message) message = body.message;
          } catch {
            message = text || t("verify.docMissing");
          }
          throw new Error(message);
        }
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
  }, [document.id, document.url, t]);

  const decisionKey = document.decision || "";
  const decisionLabel = decisionKey
    ? (() => {
        const key = `verify.docDecision.${decisionKey}`;
        const translated = t(key);
        return translated === key ? decisionKey : translated;
      })()
    : t("verify.docProvided");

  return (
    <article className="viewer">
      <div className="viewer-caption">
        <strong>{document.title}</strong>
        <span className="label">{document.fileName} · {decisionLabel || document.status || t("verify.docProvided")}</span>
      </div>
      <div className="viewer-stage">
        <div className="viewer-canvas">
          {src ? (
            <img alt={document.title} src={src} style={{ transform: `rotate(${rotation}deg) scale(${zoom})` }} />
          ) : (
            <p className="label">{error || t("verify.docLoading")}</p>
          )}
        </div>
        <div className="viewer-tools">
          <button className="ghost" type="button" disabled={locked} onClick={() => setRotation((value) => value - 90)} aria-label={t("verify.rotateLeft")}>↺</button>
          <button className="ghost" type="button" disabled={locked} onClick={() => setRotation((value) => value + 90)} aria-label={t("verify.rotateRight")}>↻</button>
          <button className="ghost" type="button" disabled={locked} onClick={() => setZoom((value) => Math.min(2.4, value + 0.2))} aria-label={t("verify.zoomIn")}>+</button>
          <button className="ghost" type="button" disabled={locked} onClick={() => setZoom((value) => Math.max(0.6, value - 0.2))} aria-label={t("verify.zoomOut")}>−</button>
          {src && !locked ? <a className="ghost tool-link" href={src} target="_blank" rel="noreferrer">{t("verify.open")}</a> : null}
          {locked ? <button className="ghost" type="button" disabled>{t("verify.open")}</button> : null}
        </div>
      </div>
      {onDecide ? (
        <div className="tabs">
          <button type="button" disabled={locked} onClick={() => onDecide(document.id, "approve")}>{t("verify.docApprove")}</button>
          <button className="ghost" type="button" disabled={locked} onClick={() => onDecide(document.id, "revision")}>{t("verify.docRevision")}</button>
          <button className="ghost" type="button" disabled={locked} onClick={() => onDecide(document.id, "reject")}>{t("verify.docReject")}</button>
        </div>
      ) : null}
    </article>
  );
}

export default function DocumentViewer({
  documents,
  onDecide,
  locked = false,
}: {
  documents: ViewerDocument[];
  onDecide?: (id: string, decision: "approve" | "reject" | "revision") => void;
  locked?: boolean;
}) {
  const { t } = useI18n();
  if (!documents.length) {
    return <p className="label">{t("verify.noDocs")}</p>;
  }
  return (
    <div className="doc-stack">
      {documents.map((document) => (
        <DocumentCard document={document} key={document.id} locked={locked} onDecide={onDecide} />
      ))}
    </div>
  );
}
