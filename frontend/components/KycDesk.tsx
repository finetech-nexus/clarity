"use client";

import { CSSProperties, FormEvent, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import DocumentViewer, { ViewerDocument } from "./DocumentViewer";
import { api, searchPage } from "../lib/api";
import { fieldLabel, useI18n } from "../lib/i18n";

type QueueItem = {
  id: string;
  workflow_id?: string;
  name: string;
  definition: string;
  definition_key?: string;
  status: string;
  state: string | null;
  documents: number;
  revisions: number;
  alert?: boolean;
  tone?: "warn" | "danger" | null;
};

type Field = { key: string; label: string; value: string; match?: boolean | null };

type OcrRead = {
  engine: string;
  confidence: number;
  production: boolean;
  extracted_text?: string;
};

type TopicDetail = { key?: string; label: string; points?: number; tier?: "critical" | "elevated" | "info" | string };

type ScreeningMatch = {
  id: string;
  status: string;
  score: number;
  name?: string;
  topics?: string[];
  topic_details?: TopicDetail[];
  datasets?: string[];
  note?: string;
  explanation?: string[] | string;
  provider?: string;
  synthetic?: boolean;
};

type Detail = {
  case: QueueItem & { task?: { key: string; name: string } | null };
  task: { key: string; name: string } | null;
  customer: {
    display_name: string;
    status: string;
    risk_tier: string | null;
    synthetic: boolean;
    client_reference?: string | null;
    external_ref?: string | null;
    individual: Record<string, string | null> | null;
    business: Record<string, string | null> | null;
    screening: Array<{ id: string; status: string; production: boolean; matches: ScreeningMatch[] }>;
    verification_checks: Array<Record<string, unknown>>;
    risk?: {
      score: number;
      tier: string;
      factors?: Array<{ code: string; points: number; explanation: string }>;
      formula_note?: string;
      methodology_version?: string;
    } | null;
  } | null;
  submitted: Field[];
  transfer?: {
    id?: string | null;
    amount?: number | string | null;
    currency?: string | null;
    counterparty_name?: string | null;
    direction?: string | null;
    description?: string | null;
    payment_reference?: string | null;
    transaction_reference?: string | null;
    account_id?: string | null;
    channel?: string | null;
    transaction_type?: string | null;
    country?: string | null;
    occurred_at?: string | null;
    fields?: Field[];
  } | null;
  identity?: Field[];
  alert?: boolean;
  alert_reasons?: string[];
  documents: Array<ViewerDocument & { extracted?: Field[]; ocr?: OcrRead | null; decision?: string | null; status?: string; type?: string; category?: string }>;
  tracker: Array<{ id?: string; type: string; category: string; country?: string; required: boolean; status: string; document_id?: string | null; label?: { fr?: string; en?: string; ar?: string } }>;
  requested_documents?: Array<{ id: string; type: string; mode: string; reason?: string; label?: { fr?: string; en?: string; ar?: string } }>;
  face_match?: { score: number | null; match: boolean | null; threshold?: number; source?: string; detail?: string } | null;
  face_threshold?: number;
  authenticity?: Array<{
    document_id: string;
    document_type: string;
    technology: string;
    manual_review: boolean;
    reasons: string[];
    document_number?: string;
    checks: Array<{ key: string; status: string; detail?: string }>;
  }>;
  steps?: Array<{ key: string; label: string; status: "done" | "current" | "upcoming" }>;
  notes?: Array<{ id: string; comment: string; actor?: string; created_at?: string; action?: string | null }>;
};

const DEFAULT_FORMULA_NOTE =
  "Score = somme des points AML + visage + authenticité des pièces + historique d'opérations + pays/structure. "
  + "AML : chaque topic a une pondération distincte (Sanctioned / Wanted / Export controlled élevés ; "
  + "Debarred / Disqualified / PEP moyens ; Person of interest / Politician informatifs). "
  + "Plusieurs topics se cumulent. Un hit sanctions reste élevé même en candidat. "
  + "Seuils : <30 faible, 30–59 moyen, ≥60 élevé.";

const TOPIC_TIER_FALLBACK: Record<string, TopicDetail["tier"]> = {
  "Export controlled": "critical",
  Sanctioned: "critical",
  Wanted: "critical",
  Debarred: "elevated",
  Disqualified: "elevated",
  PEP: "elevated",
  "Person of interest": "info",
  Politician: "info",
};

function topicRows(match: ScreeningMatch): TopicDetail[] {
  if (match.topic_details?.length) return match.topic_details;
  return (match.topics || []).map((label) => ({
    label,
    tier: TOPIC_TIER_FALLBACK[label] || "info",
  }));
}

function TopicChips({ topics }: { topics: TopicDetail[] }) {
  if (!topics.length) return <span className="label">—</span>;
  return (
    <div className="topic-chips" aria-label="Topics">
      {topics.map((topic) => (
        <span className={`topic-chip topic-${topic.tier || "info"}`} key={`${topic.label}-${topic.key || ""}`}>
          {topic.label}
          {topic.points != null ? <em>{topic.points}</em> : null}
        </span>
      ))}
    </div>
  );
}

const TAB_ALERT_REASONS: Record<string, string[]> = {
  documentData: ["correspondence"],
  documents: ["document"],
  authenticity: ["document"],
  face: ["face"],
  screening: ["screening"],
};

function tabHasAlert(tab: string, reasons: string[] | undefined) {
  const keys = TAB_ALERT_REASONS[tab] || [];
  return keys.some((key) => reasons?.includes(key));
}

const TAB_KEYS = [
  "submitted",
  "documents",
  "documentData",
  "authenticity",
  "face",
  "screening",
  "notes",
  "action",
] as const;
type TabKey = (typeof TAB_KEYS)[number];
const QUEUE_PAGE = 20;

function tabToneClass(tab: TabKey, detail: Detail | null | undefined): string {
  if (!detail) return "";
  const funding = detail.case.definition_key === "clarityFundingOrigin";
  if (tab === "documents" && !funding) {
    const toApprove = (detail.documents || []).some(
      (document) => document.status !== "requested" && document.decision !== "approved" && document.decision !== "reject" && document.decision !== "rejected",
    );
    return toApprove ? " tab-warn" : "";
  }
  if (tab === "documentData") {
    const mismatch = (detail.identity || []).some((field) => field.match === false) || tabHasAlert(tab, detail.alert_reasons);
    return mismatch ? " tab-warn" : "";
  }
  if (tab === "face" && !funding) {
    return detail.face_match == null ? " tab-warn" : "";
  }
  if (tab === "authenticity" && !funding) {
    const needsReview = (detail.authenticity || []).some(
      (row) => row.manual_review || row.checks.some((check) => check.status === "failed" || check.status === "incomplete"),
    );
    return needsReview || tabHasAlert(tab, detail.alert_reasons) ? " tab-warn" : "";
  }
  if (tab === "screening") {
    const matches = (detail.customer?.screening || []).flatMap((item) => item.matches || []);
    if (matches.some((match) => match.status === "confirmed_hit")) return " tab-danger";
    if (matches.some((match) => match.status === "pending")) return " tab-warn";
    return "";
  }
  return "";
}

function caseToneFromDetail(detail: Detail | null | undefined): "warn" | "danger" | null {
  if (!detail) return null;
  const tones = TAB_KEYS.map((tab) => tabToneClass(tab, detail));
  if (tones.some((tone) => tone.includes("danger") || tone.includes("alert"))) return "danger";
  if (tones.some((tone) => tone.includes("warn"))) return "warn";
  return null;
}

function authenticityStatusClass(status: string) {
  if (status === "passed") return "check-ok";
  if (status === "failed") return "check-bad";
  if (status === "not_applicable") return "check-wait";
  return "check-wait";
}

function AuthenticityPanel({ detail }: { detail: Detail }) {
  const { t } = useI18n();
  const rows = detail.authenticity || [];
  if (!rows.length) {
    return (
      <article className="card">
        <p className="label">{t("verify.authenticityEmpty")}</p>
      </article>
    );
  }
  return (
    <div className="stack">
      {rows.map((row) => (
        <article className="card" key={row.document_id}>
          <p className="label">{t("verify.authenticityType")}</p>
          <p>{t(`verify.docType.${row.document_type}`)}</p>
          <p className="label">{t("verify.authenticityTechnology")}</p>
          <p>{t(`verify.technology.${row.technology}`)}</p>
          {row.document_number ? <p className="label">{t("verify.authenticityNumber")} {row.document_number}</p> : null}
          {row.manual_review ? <p className="factor">{t("verify.authenticityManual")}</p> : null}
          <div className="check-list">
            {row.checks.map((check) => (
              <div className="check-row" key={check.key}>
                <span>{t(`verify.check.${check.key}`)}</span>
                <strong className={authenticityStatusClass(check.status)}>
                  {t(`verify.checkStatus.${check.status}`)}
                </strong>
              </div>
            ))}
          </div>
          {row.checks.some((check) => check.detail) ? (
            <ul className="factors">
              {row.checks.filter((check) => check.detail).map((check) => (
                <li key={`${check.key}-detail`}>{check.detail}</li>
              ))}
            </ul>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function facePercent(score: number | null | undefined) {
  if (score == null) return null;
  return Math.max(0, Math.min(100, Math.round(score <= 1 ? score * 100 : score)));
}

function FaceScale({ score, threshold }: { score: number | null | undefined; threshold: number }) {
  const { t } = useI18n();
  const value = facePercent(score);
  const cut = Math.max(0, Math.min(100, threshold));
  return (
    <div className="face-scale">
      <div className="face-track">
        <span className="face-mark" style={{ left: `${cut}%` }} />
        {value != null ? <span className="face-cursor" style={{ left: `${value}%` }}>{value}</span> : null}
      </div>
      <div className="face-axis">
        <span>0</span>
        <span className="face-threshold-label" style={{ left: `${cut}%` }}>{t("verify.faceThreshold")} {cut}</span>
        <span>100</span>
      </div>
    </div>
  );
}

function faceMatchLabel(t: (key: string) => string, match: boolean | null | undefined, comparing: boolean) {
  if (match == null) return comparing ? t("verify.faceComparing") : t("verify.faceNotDone");
  return match ? t("verify.faceOk") : t("verify.faceKo");
}

function ScoreFormulaHint({ note }: { note?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="formula-hint" onClick={(event) => event.stopPropagation()}>
      <button
        className="formula-q"
        type="button"
        aria-label="Formule du score de risque"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          setOpen((current) => !current);
        }}
      >
        ?
      </button>
      {open ? <span className="formula-note" role="note">{note || DEFAULT_FORMULA_NOTE}</span> : null}
    </span>
  );
}

function mixChannel(from: number, to: number, t: number) {
  return Math.round(from + (to - from) * t);
}

function riskScoreColor(score: number | null | undefined) {
  if (score == null || Number.isNaN(score)) return null;
  const value = Math.max(0, Math.min(100, Number(score)));
  // 0 → green, 50 → yellow, 100 → red (no blue/teal).
  const low: [number, number, number] = [52, 168, 72];
  const mid: [number, number, number] = [232, 176, 48];
  const high: [number, number, number] = [201, 76, 83];
  const [from, to, t] = value <= 50
    ? [low, mid, value / 50]
    : [mid, high, (value - 50) / 50];
  const rgb = [
    mixChannel(from[0], to[0], t),
    mixChannel(from[1], to[1], t),
    mixChannel(from[2], to[2], t),
  ];
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

function riskScoreChipStyle(score: number | null | undefined): CSSProperties | undefined {
  const color = riskScoreColor(score);
  if (!color) return undefined;
  return {
    color,
    borderColor: color,
    background: `color-mix(in srgb, ${color} 14%, white)`,
  };
}

export function RiskScoreChip({
  score,
  tier,
  note,
  factors,
  metric,
  labels,
}: {
  score?: number | null;
  tier?: string | null;
  note?: string;
  factors?: Array<{ code: string; points: number; explanation: string }>;
  metric?: boolean;
  labels?: { score: string; breakdown: string; empty: string; click: string };
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const value = score == null ? "—" : score;
  const copy = {
    score: labels?.score || t("verify.score"),
    breakdown: labels?.breakdown || t("verify.score.breakdown"),
    empty: labels?.empty || t("verify.score.empty"),
    click: labels?.click || t("verify.score.click"),
  };
  return (
    <div className={`score-chip${open ? " is-open" : ""}${metric ? " is-metric" : ""}`} style={riskScoreChipStyle(score)}>
      <button
        className="score-chip-toggle"
        type="button"
        aria-expanded={open}
        aria-label={`${copy.score}: ${value}. ${copy.click}`}
        title={copy.click}
        onClick={() => setOpen((current) => !current)}
      >
        <strong className={metric ? "metric" : undefined}>{value}</strong>
        <span className="score-chip-tier">{tier || copy.empty}</span>
      </button>
      {open ? (
        <div className="score-breakdown" role="region" aria-label={copy.breakdown}>
          <p className="label">{copy.breakdown} <ScoreFormulaHint note={note} /></p>
          {factors?.length ? (
            <ul className="factors">
              {factors.map((factor) => (
                <li key={factor.code}>{factor.points} pts — {factor.explanation}</li>
              ))}
            </ul>
          ) : (
            <p className="label">{copy.empty}</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function ScreeningMatchRow({
  match,
  pendingActions,
}: {
  match: ScreeningMatch;
  pendingActions?: { onClear: () => void; onConfirm: () => void } | null;
}) {
  const { t } = useI18n();
  const topics = topicRows(match);
  const datasets = match.datasets?.length ? match.datasets.join(", ") : "";
  const explanation = Array.isArray(match.explanation) ? match.explanation.join(" ") : match.explanation || "";
  return (
    <div className="screening-match">
      <p className="file-row">
        <span>
          <strong>{match.name || t("verify.match")}</strong>
          {" · score "}{match.score}
          {" · "}{match.status === "pending" ? t("verify.screeningPending") : match.status === "no_hit" ? t("verify.screeningNoHit") : match.status === "confirmed_hit" ? t("verify.screeningHit") : match.status}
        </span>
        {match.status === "pending" && pendingActions ? (
          <span className="tabs">
            <button type="button" onClick={pendingActions.onClear}>{t("verify.screeningFalsePositive")}</button>
            <button className="ghost" type="button" onClick={pendingActions.onConfirm}>{t("verify.screeningConfirm")}</button>
          </span>
        ) : (
          <span className="tag">{match.status === "no_hit" ? t("verify.screeningCleared") : match.status === "confirmed_hit" ? t("verify.screeningConfirmed") : match.status === "pending" ? t("verify.screeningPending") : match.status}</span>
        )}
      </p>
      <div className="screening-topics">
        <span className="label">Topics</span>
        <TopicChips topics={topics} />
      </div>
      <dl className="kv screening-detail">
        {datasets ? <span><dt>{t("verify.screeningDatasets")}</dt><dd>{datasets}</dd></span> : null}
        {match.note ? <span><dt>{t("verify.screeningDetail")}</dt><dd>{match.note}</dd></span> : null}
        {explanation ? <span><dt>{t("verify.screeningExplanation")}</dt><dd>{explanation}</dd></span> : null}
        {match.provider ? <span><dt>{t("verify.screeningSource")}</dt><dd>{match.provider}{match.synthetic ? ` · ${t("verify.screeningSynthetic")}` : ""}</dd></span> : null}
      </dl>
    </div>
  );
}

function formatNoteDate(value: string | undefined, locale: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const tag = locale === "en" ? "en-GB" : locale === "ar" ? "ar" : "fr-FR";
  return date.toLocaleString(tag, { dateStyle: "short", timeStyle: "short" });
}

function stepLabel(t: (key: string) => string, state: string | null | undefined, fallback: string) {
  if (!state) return fallback;
  const key = `verify.step.${state}`;
  const translated = t(key);
  return translated === key ? state : translated;
}

const STATUS_FILTER_VALUES = ["active", "completed", "approved-partially", "failed"] as const;

function statusLabel(t: (key: string) => string, status: string, state?: string | null) {
  if (status === "failed" || state === "rejected") return t("verify.status.failed");
  if (status === "approved-partially" || state === "approved-partially") return t("verify.status.approved-partially");
  const key = `verify.status.${status}`;
  const translated = t(key);
  if (translated !== key) return translated;
  return stepLabel(t, state, status);
}

function filtersQuery(name: string, statuses: string[], workflows: string[]) {
  const parts: string[] = [];
  const trimmed = name.trim().replaceAll('"', "");
  if (trimmed) parts.push(trimmed.includes(" ") || trimmed.includes(":") ? `name:"${trimmed}"` : `name:${trimmed}`);
  statuses.forEach((status) => parts.push(`status:${status}`));
  workflows.forEach((workflow) => parts.push(`workflow:${workflow}`));
  return parts.join(" ");
}

function taskActionLabel(t: (key: string) => string, fundingReview: boolean, reviewState: string) {
  if (fundingReview) {
    if (reviewState === "analystReview") return t("verify.transmitActivation");
    if (reviewState === "checkerReview") return t("verify.approveKyc");
  }
  if (reviewState === "analystReview") return t("verify.transmitControl");
  if (reviewState === "checkerReview") return t("verify.approve");
  if (reviewState === "investigate") return t("verify.closeInstruction");
  return "";
}

function TransferDetail({ transfer, submitted }: { transfer?: Detail["transfer"]; submitted: Field[] }) {
  const { t } = useI18n();
  const fields = transfer?.fields?.length ? transfer.fields : submitted.filter((field) =>
    ["amount", "currency", "counterpartyName", "direction", "description", "paymentReference", "transactionReference", "accountId", "channel", "transactionType", "occurredAt", "country"].includes(field.key)
  );
  const profile = submitted.filter((field) => !fields.some((row) => row.key === field.key));
  return (
    <div className="stack">
      <article className="card">
        <h3>{t("verify.transferTitle")}</h3>
        {fields.length ? (
          <dl className="kv">
            {fields.map((field) => (
              <span key={field.key}><dt>{fieldLabel(t, field.key, field.label)}</dt><dd>{field.value}</dd></span>
            ))}
          </dl>
        ) : (
          <p className="label">{t("verify.transferEmpty")}</p>
        )}
      </article>
      {profile.length ? (
        <article className="card">
          <h3>{t("verify.client")}</h3>
          <dl className="kv">
            {profile.map((field) => (
              <span key={field.key}><dt>{fieldLabel(t, field.key, field.label)}</dt><dd>{field.value}</dd></span>
            ))}
          </dl>
        </article>
      ) : null}
    </div>
  );
}

function noteActionLabel(t: (key: string) => string, action?: string | null) {
  if (!action) return "";
  if (action === "approve") return t("verify.note.approve");
  if (action === "reject") return t("verify.note.reject");
  if (action === "approvePartial" || action === "approve-partial") return t("verify.note.approvePartial");
  return action;
}

function NotesHistory({
  notes,
  freeNote,
  onFreeNote,
  onAddNote,
  canAdd,
}: {
  notes: Array<{ id: string; comment: string; actor?: string; created_at?: string; action?: string | null }>;
  freeNote: string;
  onFreeNote: (value: string) => void;
  onAddNote: () => void;
  canAdd: boolean;
}) {
  const { t, locale } = useI18n();
  return (
    <article className="card notes-panel">
      <h3>{t("verify.notesTitle")}</h3>
      {canAdd ? (
        <div className="note-compose">
          <textarea value={freeNote} onChange={(event) => onFreeNote(event.target.value)} placeholder={t("verify.notesPlaceholder")} rows={3} />
          <div className="note-compose-actions">
            <button type="button" disabled={!freeNote.trim()} onClick={onAddNote}>{t("verify.notesAdd")}</button>
          </div>
        </div>
      ) : null}
      {notes.length ? (
        <ul className="notes-list">
          {[...notes].reverse().map((note) => (
            <li key={note.id}>
              <p className="note-meta">
                <strong>{note.actor || t("verify.notesTeam")}</strong>
                {note.created_at ? <span>{formatNoteDate(note.created_at, locale)}</span> : null}
                {note.action ? <span>{noteActionLabel(t, note.action)}</span> : null}
              </p>
              <p className="note-body">{note.comment}</p>
            </li>
          ))}
        </ul>
      ) : <p className="label">{t("verify.notesEmpty")}</p>}
    </article>
  );
}

function ActionTiles({
  reviewState,
  fundingReview,
  canDecide,
  canApprove,
  finished,
  isAdmin,
  rejectNote,
  approveNote,
  partialNote,
  onRejectNote,
  onApproveNote,
  onPartialNote,
  onDecide,
  onDeleteWorkflow,
  resubmitId,
  complementTarget,
  askReason,
  sentDocuments,
  complements,
  onResubmitId,
  onComplementTarget,
  onAskReason,
  onRequestDocuments,
}: {
  reviewState: string;
  fundingReview: boolean;
  canDecide: boolean;
  canApprove: boolean;
  finished: boolean;
  isAdmin: boolean;
  rejectNote: string;
  approveNote: string;
  partialNote: string;
  onRejectNote: (value: string) => void;
  onApproveNote: (value: string) => void;
  onPartialNote: (value: string) => void;
  onDecide: (action: "approve" | "reject" | "approve-partial", note: string) => void;
  onDeleteWorkflow: () => void;
  resubmitId: string;
  complementTarget: string;
  askReason: string;
  sentDocuments: Array<{ id: string; title?: string; type?: string }>;
  complements: Array<{ type: string; category: string; country?: string; label?: { fr?: string } }>;
  onResubmitId: (value: string) => void;
  onComplementTarget: (value: string) => void;
  onAskReason: (value: string) => void;
  onRequestDocuments: (event: FormEvent, mode: "resubmit" | "additional", target: string) => void;
}) {
  const { t, locale } = useI18n();
  const isAnalyst = reviewState === "analystReview";
  const isChecker = reviewState === "checkerReview";
  const canPartial = Boolean(canDecide && isChecker && !fundingReview);
  const primaryLabel = taskActionLabel(t, fundingReview, reviewState)
    || (isAnalyst ? t("verify.transmitControl") : t("verify.approve"));
  const approveTitle = fundingReview && isChecker ? t("verify.approveKyc") : isAnalyst ? t("verify.transmit") : isChecker ? t("verify.approve") : t("verify.decision");
  const approvePlaceholder = fundingReview && isChecker ? t("verify.noteActivation") : isAnalyst ? t("verify.noteTransmit") : t("verify.noteApprove");
  return (
    <div className="stack">
      {finished ? <p className="label">{t("verify.actionNone")}</p> : (
        <div className="action-grid">
          {canDecide ? (
            <article className="card action-tile">
              <h3>{t("verify.reject")}</h3>
              <div className="form">
                <textarea value={rejectNote} onChange={(event) => onRejectNote(event.target.value)} placeholder={t("verify.rejectNote")} />
                <button className="ghost" type="button" onClick={() => onDecide("reject", rejectNote)}>{t("verify.reject")}</button>
              </div>
            </article>
          ) : null}
          {canDecide && (isAnalyst || isChecker || reviewState === "investigate") ? (
            <article className="card action-tile">
              <h3>{approveTitle}</h3>
              <div className="form">
                <textarea
                  value={approveNote}
                  onChange={(event) => onApproveNote(event.target.value)}
                  placeholder={approvePlaceholder}
                />
                <button type="button" disabled={!canApprove} onClick={() => onDecide("approve", approveNote)}>{primaryLabel}</button>
              </div>
            </article>
          ) : null}
          {canPartial ? (
            <article className="card action-tile">
              <h3>{t("verify.approvePartial")}</h3>
              <div className="form">
                <textarea value={partialNote} onChange={(event) => onPartialNote(event.target.value)} placeholder={t("verify.notePartial")} />
                <button type="button" disabled={!canApprove} onClick={() => onDecide("approve-partial", partialNote)}>{t("verify.approvePartial")}</button>
              </div>
            </article>
          ) : null}
          {canDecide && !fundingReview ? (
            <>
              <article className="card action-tile">
                <h3>{t("verify.resubmitTitle")}</h3>
                <form className="form" onSubmit={(event) => onRequestDocuments(event, "resubmit", resubmitId)}>
                  <label>
                    {t("verify.resubmitDoc")}
                    <select value={resubmitId} onChange={(event) => onResubmitId(event.target.value)} required>
                      <option value="">{t("verify.choose")}</option>
                      {sentDocuments.map((document) => (
                        <option key={document.id} value={document.id}>{document.title || document.type}</option>
                      ))}
                    </select>
                  </label>
                  <textarea value={askReason} onChange={(event) => onAskReason(event.target.value)} placeholder={t("verify.reason")} />
                  <button type="submit">{t("verify.askResubmit")}</button>
                </form>
              </article>
              <article className="card action-tile">
                <h3>{t("verify.complementTitle")}</h3>
                <form className="form" onSubmit={(event) => onRequestDocuments(event, "additional", complementTarget)}>
                  <label>
                    {t("verify.complementDoc")}
                    <select value={complementTarget} onChange={(event) => onComplementTarget(event.target.value)} required>
                      <option value="">{t("verify.choose")}</option>
                      {complements.map((row) => (
                        <option key={`${row.type}-${row.category}`} value={`${row.type}|${row.category}|${row.country || "ZZ"}`}>
                          {(row.label as Record<string, string> | undefined)?.[locale] || row.label?.fr || row.type}
                        </option>
                      ))}
                    </select>
                  </label>
                  <textarea value={askReason} onChange={(event) => onAskReason(event.target.value)} placeholder={t("verify.reason")} />
                  <button type="submit">{t("verify.askComplement")}</button>
                </form>
              </article>
            </>
          ) : null}
        </div>
      )}
      {isAdmin ? (
        <div className="action-grid">
          <article className="card action-tile">
            <h3>{t("verify.deleteWorkflow")}</h3>
            <p className="label">{t("verify.deleteWorkflowHelp")}</p>
            <div className="form">
              <button className="ghost" type="button" onClick={onDeleteWorkflow}>{t("verify.deleteWorkflow")}</button>
            </div>
          </article>
        </div>
      ) : null}
    </div>
  );
}

export default function KycDesk({ kind }: { kind?: "business" }) {
  const { t, locale } = useI18n();
  const menuId = useSearchParams().get("menu") || "";
  const [menuReady, setMenuReady] = useState(!menuId);
  const [menuTitle, setMenuTitle] = useState("");
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("status:active");
  const [nameQuery, setNameQuery] = useState("");
  const [statuses, setStatuses] = useState<string[]>(["active"]);
  const [workflows, setWorkflows] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [tab, setTab] = useState<TabKey>("submitted");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [reading, setReading] = useState(false);
  const [resubmitId, setResubmitId] = useState("");
  const [complementTarget, setComplementTarget] = useState("");
  const [askReason, setAskReason] = useState("");
  const [comparing, setComparing] = useState(false);
  const [faceTried, setFaceTried] = useState("");
  const [rejectNote, setRejectNote] = useState("");
  const [approveNote, setApproveNote] = useState("");
  const [partialNote, setPartialNote] = useState("");
  const [freeNote, setFreeNote] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const previousMenu = useRef<string | null>(null);

  useEffect(() => {
    void api<{ role?: string; roles?: string[] }>("/api/clarity/v1/me")
      .then((me) => {
        const roles = Array.isArray(me.roles) ? me.roles.map(String) : [];
        setIsAdmin(me.role === "admin" || roles.includes("admin"));
      })
      .catch(() => setIsAdmin(false));
  }, []);

  async function loadQueue(prefer?: string, nextQuery = query, nextPage = page, append = false) {
    const body = await searchPage<QueueItem>(
      kind ? `/api/clarity/v1/desks/kyc?kind=${kind}` : "/api/clarity/v1/desks/kyc",
      nextQuery,
      nextPage,
      QUEUE_PAGE,
    );
    setTotal(body.total);
    setQueue((current) => {
      if (!append || nextPage === 0) return body.items;
      const seen = new Set(current.map((row) => row.id));
      return [...current, ...body.items.filter((row) => !seen.has(row.id))];
    });
    const requested = new URLSearchParams(window.location.search).get("case") || "";
    setSelected((current) => {
      if (prefer) return prefer;
      if (requested && body.items.some((row) => row.id === requested)) return requested;
      if (current && (append ? true : body.items.some((row) => row.id === current))) {
        if (append) return current;
        if (body.items.some((row) => row.id === current)) return current;
      }
      if (append && current) return current;
      return body.items[0]?.id || "";
    });
  }

  function applyCaseColors(runtimeId: string, updated: Detail) {
    const tone = caseToneFromDetail(updated) || updated.case.tone || null;
    setQueue((current) =>
      current.map((item) =>
        item.id !== runtimeId
          ? item
          : {
              ...item,
              alert: tone === "danger" || Boolean(updated.alert),
              tone,
              documents: updated.documents?.length ?? item.documents,
              revisions: updated.documents?.filter((doc) => doc.decision === "revisions").length ?? item.revisions,
              status: updated.case.status,
              state: updated.case.state,
            }
      )
    );
  }

  async function refreshSelectedCase(runtimeId = selected) {
    if (!runtimeId) return null;
    const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`);
    setDetail(updated);
    applyCaseColors(runtimeId, updated);
    return updated;
  }

  useEffect(() => {
    const leftMenu = Boolean(previousMenu.current) && !menuId;
    previousMenu.current = menuId;
    if (!menuId) {
      setMenuTitle("");
      if (leftMenu) {
        setWorkflows([]);
        setQuery(filtersQuery(nameQuery, statuses, []));
      }
      setMenuReady(true);
      return;
    }
    setMenuReady(false);
    void api<{ label_fr: string; label_en: string; process_keys: string[] }>(`/api/clarity/v1/menus/${menuId}`)
      .then((menu) => {
        const keys = menu.process_keys || [];
        setMenuTitle(locale === "fr" ? menu.label_fr : menu.label_en || menu.label_fr);
        setWorkflows(keys);
        setPage(0);
        setQuery(filtersQuery(nameQuery, statuses, keys));
        setMenuReady(true);
      })
      .catch((err: Error) => {
        setError(err.message);
        setMenuReady(true);
      });
  }, [menuId, locale]);

  useEffect(() => {
    if (!menuReady) return;
    setPage(0);
    void loadQueue(undefined, query, 0, false).catch((err: Error) => setError(err.message));
  }, [query, kind, menuReady]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    void api<Detail>(`/api/clarity/v1/desks/kyc/${selected}`)
      .then((updated) => {
        setDetail(updated);
        applyCaseColors(selected, updated);
      })
      .catch((err: Error) => setError(err.message));
  }, [selected]);

  async function deleteWorkflow() {
    if (!selected) return;
    if (!window.confirm(t("verify.deleteWorkflowConfirm"))) return;
    setError("");
    try {
      await api(`/api/clarity/v1/desks/kyc/${selected}`, { method: "DELETE" });
      setDetail(null);
      setSelected("");
      setMessage(t("verify.deleteWorkflowDone"));
      await loadQueue();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("verify.deleteWorkflow"));
    }
  }

  async function decideCase(action: "approve" | "reject" | "revision" | "approve-partial", note = "") {
    setError("");
    try {
      const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/decision`, {
        method: "POST",
        body: JSON.stringify({ action, rationale: note }),
      });
      setDetail(updated);
      if (selected) applyCaseColors(selected, updated);
      setRejectNote("");
      setApproveNote("");
      setPartialNote("");
      setMessage(action === "revision" ? "Renvoi demandé. Le client doit fournir les pièces marquées." : "Décision enregistrée sur le dossier.");
      await loadQueue(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision refusée");
    }
  }

  async function addCaseNote() {
    if (!selected || !freeNote.trim()) return;
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/notes`, {
        method: "POST",
        body: JSON.stringify({ comment: freeNote }),
      }));
      setFreeNote("");
      setMessage("Note enregistrée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Note refusée");
    }
  }

  const identityKey = (detail?.identity || []).map((field) => `${field.key}:${field.value}`).join("|");

  useEffect(() => {
    if (!detail) return;
    const next: Record<string, string> = {};
    for (const field of detail.identity || []) next[field.key] = field.value;
    setDraft(next);
  }, [selected, identityKey]);

  async function readIdentity() {
    setError("");
    setReading(true);
    try {
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/identity/ocr`, { method: "POST" });
      setDetail(refreshed);
      if (selected) applyCaseColors(selected, refreshed);
      const found = (refreshed.identity || []).some((field) => ["firstName", "lastName", "dateOfBirth"].includes(field.key) && field.value);
      setMessage(found ? "Lecture enregistrée." : "Aucune identité lue sur la carte ou le passeport.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Lecture refusée");
    } finally {
      setReading(false);
    }
  }

  async function saveIdentity(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/identity`, {
        method: "PATCH",
        body: JSON.stringify({ fields: draft }),
      });
      setDetail(refreshed);
      if (selected) applyCaseColors(selected, refreshed);
      setMessage("Données d'identité enregistrées.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function disposition(id: string, status: "no_hit" | "confirmed_hit") {
    setError("");
    try {
      await api(`/api/clarity/v1/screening/matches/${id}/disposition`, { method: "POST", body: JSON.stringify({ status }) });
      await refreshSelectedCase();
      setMessage(status === "no_hit" ? "Correspondance écartée." : "Correspondance confirmée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vérification du criblage refusée");
    }
  }

  async function requestDocuments(event: FormEvent, mode: "resubmit" | "additional", target: string) {
    event.preventDefault();
    if (!selected || !target) return;
    setError("");
    const item = mode === "resubmit"
      ? { document_id: target }
      : { type: target.split("|")[0], category: target.split("|")[1], country: target.split("|")[2] || "ZZ" };
    try {
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/document-requests`, {
        method: "POST",
        body: JSON.stringify({ reason: askReason, items: [item] }),
      });
      setDetail(refreshed);
      if (selected) applyCaseColors(selected, refreshed);
      setAskReason("");
      setResubmitId("");
      setComplementTarget("");
      setMessage(mode === "resubmit" ? "Renvoi demandé au client." : "Pièce complémentaire demandée au client.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demande refusée");
    }
  }

  async function compareFaces() {
    if (!selected) return;
    setError("");
    setComparing(true);
    try {
      const refreshed = await api<Detail>(`/api/clarity/v1/desks/kyc/${selected}/face-match`, { method: "POST" });
      setDetail(refreshed);
      applyCaseColors(selected, refreshed);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Comparaison des visages refusée");
    } finally {
      setComparing(false);
    }
  }

  async function decideDocument(id: string, decision: "approve" | "reject" | "revision") {
    setError("");
    try {
      await api<ViewerDocument>(`/api/clarity/v1/desks/documents/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await refreshSelectedCase();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision document refusée");
    }
  }

  const taskKey = detail?.task?.key || "";
  const matches = (detail?.customer?.screening || []).flatMap((item) => item.matches || []);
  const piecesApproved = Boolean(detail?.documents.length) && detail!.documents.every((item) => item.decision === "approved");
  const screeningPending = matches.some((item) => item.status === "pending");
  const fundingReview = detail?.case.definition_key === "clarityFundingOrigin";
  const reviewState = taskKey || detail?.case.state || "";
  const canDecide = reviewState === "analystReview" || reviewState === "checkerReview" || reviewState === "investigate";
  const canApprove = canDecide && (fundingReview || piecesApproved) && !screeningPending;
  const finished = !detail || detail.case.status === "failed" || detail.case.state === "approved" || detail.case.state === "approved-partially" || detail.case.state === "rejected" || detail.case.state === "closed";
  const sentDocuments = (detail?.documents || []).filter((document) => document.status !== "requested");
  const complements = (detail?.tracker || []).filter((row) => !row.document_id);
  const screeningFactors = (detail?.customer?.risk?.factors || []).filter((factor) =>
    factor.code === "screening_confirmed" || factor.code === "screening_candidate"
  );
  const selectedIndex = queue.findIndex((item) => item.id === selected);
  const canPrevCase = selectedIndex > 0;
  const canNextCase = selectedIndex >= 0 && selectedIndex < queue.length - 1;

  useEffect(() => {
    setFaceTried("");
  }, [selected]);

  useEffect(() => {
    if (tab !== "face" || !selected || !detail || detail.face_match || faceTried === selected || comparing) return;
    setFaceTried(selected);
    void compareFaces();
  }, [tab, selected, detail, faceTried, comparing]);

  return (
    <>
      <h1>{menuTitle || t("verify.title")}</h1>
      <p className="lede">{t("verify.lede")}</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <div className="filters">
        <div className="filter-group">
          <span className="label">{t("verify.filterName")}</span>
          <input
            aria-label={t("verify.filterName")}
            placeholder={t("verify.searchPlaceholder")}
            value={nameQuery}
            onChange={(event) => {
              const next = event.target.value;
              setNameQuery(next);
              setPage(0);
              setQuery(filtersQuery(next, statuses, workflows));
            }}
          />
        </div>
        <div className="filter-group">
          <span className="label">{t("verify.filterStatus")}</span>
          <div className="checks" role="group" aria-label={t("verify.filterStatus")}>
            {STATUS_FILTER_VALUES.map((value) => (
              <label key={value}>
                <input
                  type="checkbox"
                  checked={statuses.includes(value)}
                  onChange={() => {
                    const next = statuses.includes(value) ? statuses.filter((item) => item !== value) : [...statuses, value];
                    setStatuses(next);
                    setPage(0);
                    setQuery(filtersQuery(nameQuery, next, workflows));
                  }}
                />
                {t(`verify.status.${value}`)}
              </label>
            ))}
          </div>
        </div>
      </div>
      <p className="label">{queue.length}/{total} {t("verify.cases")}</p>
      <div className="kyc-shell">
        <aside className="queue-panel" aria-label={t("verify.queue")}>
          <div className="panel-head">
            <h3>{t("verify.queue")}</h3>
            <span className="label">{queue.length}</span>
          </div>
          <div className="queue">
            {queue.map((item) => (
              <button
                className={`${item.id === selected ? "active" : "ghost"}${item.tone === "danger" || item.alert ? " tone-danger" : item.tone === "warn" ? " tone-warn" : ""}`}
                key={item.id}
                type="button"
                onClick={() => setSelected(item.id)}
              >
                <span>{item.name}</span>
                <span className="label">{item.status === "active" ? stepLabel(t, item.state, item.status) : statusLabel(t, item.status, item.state)} · {item.documents} {t("verify.pieces")}{item.revisions ? ` · ${item.revisions} ${t("verify.revisions")}` : ""}</span>
              </button>
            ))}
          </div>
          {queue.length < total ? (
            <div className="load-more">
              <button
                className="ghost"
                type="button"
                onClick={() => {
                  const next = page + 1;
                  setPage(next);
                  void loadQueue(undefined, query, next, true);
                }}
              >
                {t("verify.loadMore")}
              </button>
            </div>
          ) : null}
          <div className="queue-nav">
            <button
              className="ghost"
              type="button"
              disabled={!canPrevCase}
              onClick={() => {
                if (canPrevCase) setSelected(queue[selectedIndex - 1].id);
              }}
            >
              {t("verify.prev")}
            </button>
            <button
              className="ghost"
              type="button"
              disabled={!canNextCase}
              onClick={() => {
                if (canNextCase) setSelected(queue[selectedIndex + 1].id);
              }}
            >
              {t("verify.next")}
            </button>
          </div>
        </aside>
        {detail ? (
          <div className="case-panel case-panel-score" aria-label={t("verify.detail")}>
          <RiskScoreChip
            score={detail.customer?.risk?.score}
            tier={detail.customer?.risk?.tier}
            note={detail.customer?.risk?.formula_note}
            factors={detail.customer?.risk?.factors || []}
            labels={{
              score: t("verify.score"),
              breakdown: t("verify.score.breakdown"),
              empty: t("verify.score.empty"),
              click: t("verify.score.click"),
            }}
          />
          <div className="case-sheet">
            <header className="case-bar">
              <div>
                <p className="panel-kicker">{t("verify.detail")}</p>
                <h2>{detail.customer?.display_name || detail.case.name}</h2>
                <p className="label" style={{ marginTop: 4, marginBottom: 0 }}>
                  id-verification = {detail.case.id}
                </p>
                <p
                  className="label"
                  style={{ marginTop: 2, marginBottom: 0, fontSize: 12, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}
                >
                  external-id = {detail.customer?.client_reference || "—"}
                </p>
                <p className="label" style={{ marginTop: 2, marginBottom: 0 }}>
                  workflow = {detail.case.definition || detail.case.definition_key || "—"}
                </p>
                <p className="label">{statusLabel(t, detail.case.status, detail.case.state)} · {detail.task?.name || stepLabel(t, detail.case.state, t("verify.noTask"))}</p>
                {detail.steps?.length ? (
                  <ol className="stepper">
                    {detail.steps.map((step) => (
                      <li className={step.status} key={step.key}><span className="dot" />{stepLabel(t, step.key, step.label)}</li>
                    ))}
                  </ol>
                ) : null}
              </div>
            </header>
            <div className="case-layout">
            <div className="tabs case-tabs">
              {TAB_KEYS.map((item) => (
                <button
                  className={`${tab === item ? "active" : ""}${tabToneClass(item, detail)}`}
                  key={item}
                  type="button"
                  onClick={() => setTab(item)}
                >
                  {t(`verify.tab.${item}`)}
                </button>
              ))}
            </div>
            <div className="case-pane">
            {tab === "submitted" ? (
              fundingReview ? (
                <TransferDetail transfer={detail.transfer} submitted={detail.submitted || []} />
              ) : (
                <article className="card">
                  {detail.submitted?.length ? (
                    <dl className="kv">
                      {detail.submitted.map((field) => (
                        <span key={field.key}><dt>{fieldLabel(t, field.key, field.label)}</dt><dd>{field.value}</dd></span>
                      ))}
                    </dl>
                  ) : <p className="label">{t("verify.noSubmitted")}</p>}
                </article>
              )
            ) : null}
            {tab === "documents" ? (
              <div className="document-data">
                <h3>{t("verify.docsList")}</h3>
                {!detail.documents.length ? (
                  <p className="label">
                    {fundingReview
                      ? t("verify.noDocsFunding")
                      : detail.case.state === "collect"
                      ? t("verify.noDocsCollect")
                      : t("verify.noDocs")}
                  </p>
                ) : null}
                <DocumentViewer
                  documents={detail.documents}
                  locked={Boolean(finished) || fundingReview}
                  onDecide={finished || fundingReview ? undefined : (id, decision) => void decideDocument(id, decision)}
                />
              </div>
            ) : null}
            {tab === "documentData" ? (
              <article className="card">
                {detail.identity?.length ? (
                  <form className="form" onSubmit={(event) => void saveIdentity(event)}>
                    {detail.identity.map((field) => (
                      <label className={field.match === false ? "mismatch" : ""} key={field.key}>
                        {fieldLabel(t, field.key, field.label)}
                        <input
                          value={draft[field.key] ?? field.value}
                          onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
                        />
                      </label>
                    ))}
                    <div className="actions">
                      <button className="ghost" type="button" disabled={reading} onClick={() => void readIdentity()}>
                        {reading ? t("verify.reading") : t("verify.readDocs")}
                      </button>
                      <button type="submit">{t("verify.save")}</button>
                    </div>
                  </form>
                ) : <p className="label">{t("verify.noIdentity")}</p>}
              </article>
            ) : null}
            {tab === "authenticity" ? <AuthenticityPanel detail={detail} /> : null}
            {tab === "face" ? (
              <article className="card">
                <FaceScale score={detail.face_match?.score} threshold={detail.face_threshold ?? detail.face_match?.threshold ?? 40} />
                <p>{faceMatchLabel(t, detail.face_match?.match, comparing)}</p>
                <p className="label">{detail.face_match?.source === "video" ? t("verify.faceVideo") : t("verify.faceSelfie")}</p>
                {detail.face_match?.detail ? <p className="label">{detail.face_match.detail}</p> : null}
                <button type="button" disabled={comparing} onClick={() => void compareFaces()}>{t("verify.faceAgain")}</button>
              </article>
            ) : null}
            {tab === "screening" ? (
              <article className="card">
                {screeningFactors.map((factor) => (
                  <p className="factor" key={factor.code}>{factor.points} pts — {factor.explanation}</p>
                ))}
                {matches.length ? matches.map((match) => (
                  <ScreeningMatchRow
                    key={match.id}
                    match={match}
                    pendingActions={match.status === "pending" ? {
                      onClear: () => void disposition(match.id, "no_hit"),
                      onConfirm: () => void disposition(match.id, "confirmed_hit"),
                    } : null}
                  />
                )) : (
                  <p className="label">
                    {detail.case.state === "collect"
                      ? t("verify.screeningCollect")
                      : t("verify.screeningNone")}
                  </p>
                )}
              </article>
            ) : null}
            {tab === "notes" ? (
              <NotesHistory
                notes={detail.notes || []}
                freeNote={freeNote}
                onFreeNote={setFreeNote}
                onAddNote={() => void addCaseNote()}
                canAdd={!finished}
              />
            ) : null}
            {tab === "action" ? (
              <div className="stack">
                {!canDecide && !finished ? (
                  <p className="label">
                    {detail.case.state === "collect"
                      ? t("verify.actionNoneCollect")
                      : t("verify.actionNoneStep", { step: stepLabel(t, detail.case.state, detail.task?.name || t("verify.status.active")) })}
                  </p>
                ) : null}
                {fundingReview && !finished ? <p className="label">{t("verify.actionFunding")}</p> : null}
                {canDecide && !piecesApproved && !fundingReview && !finished ? <p className="error">{t("verify.actionNeedDocs")}</p> : null}
                {canDecide && screeningPending && !finished ? <p className="label">{t("verify.actionNeedScreening")}</p> : null}
                <ActionTiles
                  reviewState={reviewState}
                  fundingReview={Boolean(fundingReview)}
                  canDecide={Boolean(canDecide)}
                  canApprove={Boolean(canApprove)}
                  finished={Boolean(finished)}
                  isAdmin={isAdmin}
                  rejectNote={rejectNote}
                  approveNote={approveNote}
                  partialNote={partialNote}
                  onRejectNote={setRejectNote}
                  onApproveNote={setApproveNote}
                  onPartialNote={setPartialNote}
                  onDecide={(action, note) => void decideCase(action, note)}
                  onDeleteWorkflow={() => void deleteWorkflow()}
                  resubmitId={resubmitId}
                  complementTarget={complementTarget}
                  askReason={askReason}
                  sentDocuments={sentDocuments}
                  complements={complements}
                  onResubmitId={setResubmitId}
                  onComplementTarget={setComplementTarget}
                  onAskReason={setAskReason}
                  onRequestDocuments={(event, mode, target) => void requestDocuments(event, mode, target)}
                />
                {detail.requested_documents?.length ? (
                  <ul className="factors">
                    {detail.requested_documents.map((item) => (
                      <li key={item.id}>{(item.label as any)?.[locale] || item.label?.fr || item.type} — {item.mode === "resubmit" ? t("verify.requestedResubmit") : t("verify.requestedExtra")}{item.reason ? ` · ${item.reason}` : ""}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
            </div>
            </div>
          </div>
          </div>
        ) : selected ? <p>{t("verify.loadingCase")}</p> : null}
      </div>
    </>
  );
}

export function VerificationPreview({ runtimeId, onChanged }: { runtimeId: string; onChanged?: () => void }) {
  const { t, locale } = useI18n();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [tab, setTab] = useState<TabKey>("submitted");
  const [comparing, setComparing] = useState(false);
  const [faceTried, setFaceTried] = useState("");
  const [resubmitId, setResubmitId] = useState("");
  const [complementTarget, setComplementTarget] = useState("");
  const [askReason, setAskReason] = useState("");
  const [rejectNote, setRejectNote] = useState("");
  const [approveNote, setApproveNote] = useState("");
  const [partialNote, setPartialNote] = useState("");
  const [freeNote, setFreeNote] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    void api<{ role?: string; roles?: string[] }>("/api/clarity/v1/me")
      .then((me) => {
        const roles = Array.isArray(me.roles) ? me.roles.map(String) : [];
        setIsAdmin(me.role === "admin" || roles.includes("admin"));
      })
      .catch(() => setIsAdmin(false));
  }, []);

  useEffect(() => {
    setDetail(null);
    setError("");
    setMessage("");
    setTab("submitted");
    setFaceTried("");
    setResubmitId("");
    setComplementTarget("");
    setAskReason("");
    setRejectNote("");
    setApproveNote("");
    setPartialNote("");
    setFreeNote("");
    void api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`).then(setDetail).catch((err: Error) => setError(err.message));
  }, [runtimeId]);

  async function refreshPreview() {
    const updated = await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}`);
    setDetail(updated);
    onChanged?.();
    return updated;
  }

  async function compareFaces() {
    setError("");
    setComparing(true);
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/face-match`, { method: "POST" }));
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Comparaison des visages refusée");
    } finally {
      setComparing(false);
    }
  }

  useEffect(() => {
    if (tab !== "face" || !detail || detail.face_match || faceTried === runtimeId || comparing) return;
    setFaceTried(runtimeId);
    void compareFaces();
  }, [tab, detail, faceTried, comparing, runtimeId]);

  async function requestDocuments(event: FormEvent, mode: "resubmit" | "additional", target: string) {
    event.preventDefault();
    if (!target) return;
    setError("");
    const item = mode === "resubmit"
      ? { document_id: target }
      : { type: target.split("|")[0], category: target.split("|")[1], country: target.split("|")[2] || "ZZ" };
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/document-requests`, {
        method: "POST",
        body: JSON.stringify({ reason: askReason, items: [item] }),
      }));
      setAskReason("");
      setResubmitId("");
      setComplementTarget("");
      setMessage(mode === "resubmit" ? "Renvoi demandé au client." : "Pièce complémentaire demandée au client.");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demande refusée");
    }
  }

  async function decideCase(action: "approve" | "reject" | "approve-partial", note = "") {
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/decision`, {
        method: "POST",
        body: JSON.stringify({ action, rationale: note }),
      }));
      setRejectNote("");
      setApproveNote("");
      setPartialNote("");
      setMessage(action === "approve-partial" ? "Approbation partielle enregistrée." : "Décision enregistrée sur le dossier.");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision refusée");
    }
  }

  async function deleteWorkflow() {
    if (!window.confirm(t("verify.deleteWorkflowConfirm"))) return;
    setError("");
    try {
      await api(`/api/clarity/v1/desks/kyc/${runtimeId}`, { method: "DELETE" });
      setDetail(null);
      setMessage(t("verify.deleteWorkflowDone"));
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("verify.deleteWorkflow"));
    }
  }

  async function decideDocument(id: string, decision: "approve" | "reject" | "revision") {
    setError("");
    try {
      await api<ViewerDocument>(`/api/clarity/v1/desks/documents/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await refreshPreview();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Décision document refusée");
    }
  }

  async function disposition(id: string, status: "no_hit" | "confirmed_hit") {
    setError("");
    try {
      await api(`/api/clarity/v1/screening/matches/${id}/disposition`, { method: "POST", body: JSON.stringify({ status }) });
      await refreshPreview();
      setMessage(status === "no_hit" ? "Correspondance écartée." : "Correspondance confirmée.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vérification du criblage refusée");
    }
  }

  async function addCaseNote() {
    if (!freeNote.trim()) return;
    setError("");
    try {
      setDetail(await api<Detail>(`/api/clarity/v1/desks/kyc/${runtimeId}/notes`, {
        method: "POST",
        body: JSON.stringify({ comment: freeNote }),
      }));
      setFreeNote("");
      setMessage("Note enregistrée.");
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Note refusée");
    }
  }

  if (error && !detail) return <p className="error">{error}</p>;
  if (!detail) return <p className="label">{t("verify.loadingVerification")}</p>;
  const matches = (detail.customer?.screening || []).flatMap((item) => item.matches || []);
  const taskKey = detail.task?.key || "";
  const fundingReview = detail.case.definition_key === "clarityFundingOrigin";
  const reviewState = taskKey || detail.case.state || "";
  const piecesApproved = Boolean(detail.documents.length) && detail.documents.every((item) => item.decision === "approved");
  const screeningPending = matches.some((item) => item.status === "pending");
  const canDecide = reviewState === "analystReview" || reviewState === "checkerReview" || reviewState === "investigate";
  const canApprove = canDecide && (fundingReview || piecesApproved) && !screeningPending;
  const finished = detail.case.status === "failed" || detail.case.state === "approved" || detail.case.state === "approved-partially" || detail.case.state === "rejected" || detail.case.state === "closed";
  const docsLocked = finished || !canDecide;
  const sentDocuments = (detail.documents || []).filter((document) => document.status !== "requested");
  const complements = (detail.tracker || []).filter((row) => !row.document_id);
  return (
    <div className="verify-preview">
      <aside className="verify-side">
        <div className="tabs case-tabs">
          {TAB_KEYS.map((item) => (
            <button
              className={`${tab === item ? "active" : ""}${tabToneClass(item, detail)}`}
              key={item}
              type="button"
              onClick={() => setTab(item)}
            >
              {t(`verify.tab.${item}`)}
            </button>
          ))}
        </div>
      </aside>
      <div>
        {error ? <p className="error">{error}</p> : null}
        {message ? <p className="label">{message}</p> : null}
        {tab === "submitted" ? (
          fundingReview ? (
            <TransferDetail transfer={detail.transfer} submitted={detail.submitted || []} />
          ) : (
            <article className="card">
              {detail.submitted?.length ? (
                <dl className="kv">
                  {detail.submitted.map((field) => (
                    <span key={field.key}><dt>{fieldLabel(t, field.key, field.label)}</dt><dd>{field.value}</dd></span>
                  ))}
                </dl>
              ) : <p className="label">{t("verify.noSubmitted")}</p>}
            </article>
          )
        ) : null}
        {tab === "documents" ? (
          <DocumentViewer
            documents={detail.documents}
            locked={docsLocked || fundingReview}
            onDecide={docsLocked || fundingReview ? undefined : (id, decision) => void decideDocument(id, decision)}
          />
        ) : null}
        {tab === "documentData" ? (
          <article className="card">
            {detail.identity?.length ? (
              <form className="form">
                {detail.identity.map((field) => (
                  <label key={field.key}>
                    {fieldLabel(t, field.key, field.label)}
                    <input disabled readOnly value={field.value} />
                  </label>
                ))}
                <div className="actions">
                  <button className="ghost" type="button" disabled>{t("verify.readDocs")}</button>
                  <button type="button" disabled>{t("verify.save")}</button>
                </div>
              </form>
            ) : <p className="label">{t("verify.noIdentity")}</p>}
          </article>
        ) : null}
        {tab === "authenticity" ? <AuthenticityPanel detail={detail} /> : null}
        {tab === "face" ? (
          <article className="card">
            <FaceScale score={detail.face_match?.score} threshold={detail.face_threshold ?? detail.face_match?.threshold ?? 40} />
            <p>{faceMatchLabel(t, detail.face_match?.match, comparing)}</p>
            <p className="label">{detail.face_match?.source === "video" ? t("verify.faceVideo") : t("verify.faceSelfie")}</p>
            {detail.face_match?.detail ? <p className="label">{detail.face_match.detail}</p> : null}
            <button type="button" disabled={comparing} onClick={() => void compareFaces()}>{t("verify.faceAgain")}</button>
          </article>
        ) : null}
        {tab === "screening" ? (
          <article className="card">
            {(detail.customer?.risk?.factors || [])
              .filter((factor) => factor.code === "screening_confirmed" || factor.code === "screening_candidate")
              .map((factor) => (
                <p className="factor" key={factor.code}>{factor.points} pts — {factor.explanation}</p>
              ))}
            {matches.length ? matches.map((match) => (
              <ScreeningMatchRow
                key={match.id}
                match={match}
                pendingActions={
                  !finished && match.status === "pending"
                    ? {
                        onClear: () => void disposition(match.id, "no_hit"),
                        onConfirm: () => void disposition(match.id, "confirmed_hit"),
                      }
                    : null
                }
              />
            )) : <p className="label">{t("verify.screeningNone")}</p>}
          </article>
        ) : null}
        {tab === "notes" ? (
          <NotesHistory
            notes={detail.notes || []}
            freeNote={freeNote}
            onFreeNote={setFreeNote}
            onAddNote={() => void addCaseNote()}
            canAdd={!finished}
          />
        ) : null}
        {tab === "action" ? (
          <div className="stack">
            {!canDecide && !finished ? (
              <p className="label">
                {detail.case.state === "collect"
                  ? t("verify.actionNoneCollect")
                  : t("verify.actionNoneStep", { step: stepLabel(t, detail.case.state, detail.task?.name || t("verify.status.active")) })}
              </p>
            ) : null}
            {fundingReview && !finished ? <p className="label">{t("verify.actionFunding")}</p> : null}
            {canDecide && !piecesApproved && !fundingReview && !finished ? <p className="error">{t("verify.actionNeedDocs")}</p> : null}
            {canDecide && screeningPending && !finished ? <p className="label">{t("verify.actionNeedScreening")}</p> : null}
            <ActionTiles
              reviewState={reviewState}
              fundingReview={Boolean(fundingReview)}
              canDecide={Boolean(canDecide)}
              canApprove={Boolean(canApprove)}
              finished={Boolean(finished)}
              isAdmin={isAdmin}
              rejectNote={rejectNote}
              approveNote={approveNote}
              partialNote={partialNote}
              onRejectNote={setRejectNote}
              onApproveNote={setApproveNote}
              onPartialNote={setPartialNote}
              onDecide={(action, note) => void decideCase(action, note)}
              onDeleteWorkflow={() => void deleteWorkflow()}
              resubmitId={resubmitId}
              complementTarget={complementTarget}
              askReason={askReason}
              sentDocuments={sentDocuments}
              complements={complements}
              onResubmitId={setResubmitId}
              onComplementTarget={setComplementTarget}
              onAskReason={setAskReason}
              onRequestDocuments={(event, mode, target) => void requestDocuments(event, mode, target)}
            />
            {detail.requested_documents?.length ? (
              <ul className="factors">
                {detail.requested_documents.map((item) => (
                  <li key={item.id}>{(item.label as any)?.[locale] || item.label?.fr || item.type} — {item.mode === "resubmit" ? t("verify.requestedResubmit") : t("verify.requestedExtra")}{item.reason ? ` · ${item.reason}` : ""}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
