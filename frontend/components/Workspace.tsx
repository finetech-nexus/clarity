"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import CaseDesk from "./CaseDesk";
import KycDesk from "./KycDesk";
import ScreeningDesk from "./ScreeningDesk";
import StudioDesk from "./StudioDesk";
import WorkflowEditor from "./WorkflowEditor";
import { AdminBoard, AlertBoard, AuditBoard, IntegrationBoard, ReportBoard, ReviewBoard, RiskBoard, TransactionBoard } from "./Workboards";
import { QueryStack } from "./QueryStack";
import { api, searchPage } from "../lib/api";

const GROUPS = [
  {
    id: "kyc",
    label: "KYC",
    links: [
      ["/", "Vue d'ensemble"],
      ["/customers", "Clients"],
      ["/kyc-sessions", "Sessions"],
      ["/verification", "Vérification"],
    ],
  },
  {
    id: "kyb",
    label: "KYB",
    links: [
      ["/kyb", "Vue d'ensemble"],
      ["/businesses", "Clients"],
      ["/kyb-sessions", "Sessions"],
    ],
  },
  {
    id: "aml",
    label: "AML",
    links: [
      ["/aml", "Vue d'ensemble"],
      ["/screening", "Criblage"],
      ["/risk", "Risque"],
      ["/transactions", "Transactions"],
      ["/alerts", "Alertes"],
      ["/investigations", "Investigations"],
      ["/reviews", "Revues"],
      ["/reports", "Rapports"],
    ],
  },
  {
    id: "settings",
    label: "Paramétrage",
    links: [
      ["/rules", "Scénarios"],
      ["/workflows", "Parcours"],
      ["/audit", "Audit"],
      ["/integrations", "Intégrations"],
      ["/administration", "Administration"],
    ],
  },
];

function isCurrent(pathname: string, href: string, customerKind?: string) {
  if (pathname.startsWith("/customers/") && customerKind) {
    if (href === "/businesses") return customerKind === "business";
    if (href === "/customers") return customerKind !== "business";
  }
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

type Customer = {
  id: string;
  display_name: string;
  kind: string;
  status: string;
  kyc_status?: string;
  kyc_label?: string;
  risk_tier: string | null;
  synthetic: boolean;
  external_ref?: string | null;
  kyc_created_at?: string | null;
  kyc_validated_at?: string | null;
  kyc_expires_at?: string | null;
  reminder?: string;
  reminded_at?: string | null;
};

type KycCycle = {
  id: string;
  status: string;
  label: string;
  pending: boolean;
  created_at: string | null;
  validated_at: string | null;
  expires_at: string | null;
  reminder: string;
  reminded_at: string | null;
  production: boolean;
};

export default function Workspace({ section, customerId }: { section: string; customerId?: string }) {
  const pathname = usePathname();
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("camille.morel@clarity.bank");
  const [password, setPassword] = useState("Clarity-demo-2026!");
  const [error, setError] = useState("");
  const [data, setData] = useState<unknown>(null);
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    setToken(sessionStorage.getItem("clarity_token"));
  }, []);

  useEffect(() => {
    if (!token) return;
    const load = async () => {
      setError("");
      try {
        if (customerId) {
          setProfile(await api(`/api/clarity/v1/customers/${customerId}`));
          return;
        }
        const routes: Record<string, string> = {
          overview: "/api/clarity/v1/dashboard",
          kyb: "/api/clarity/v1/dashboard",
          aml: "/api/clarity/v1/dashboard",
        };
        if (!routes[section]) {
          setData(null);
          return;
        }
        setData(await api(routes[section]));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Erreur de chargement");
      }
    };
    void load();
  }, [token, section, customerId]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const body = await api<{ access_token: string }>("/api/clarity/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      sessionStorage.setItem("clarity_token", body.access_token);
      setToken(body.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connexion refusée");
    }
  }

  if (!token) {
    return (
      <form className="login" onSubmit={login}>
        <div className="brand">Clarity<span>KYC et AML bancaires</span></div>
        <p className="lede">Les comptes et les listes de cet environnement sont synthétiques.</p>
        <input aria-label="E-mail" value={email} onChange={(event) => setEmail(event.target.value)} />
        <input aria-label="Mot de passe" type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
        {error && <p className="error">{error}</p>}
        <button type="submit">Entrer</button>
      </form>
    );
  }

  return (
    <div className="shell">
      <aside className="nav">
        <Link href="/" className="brand">Clarity<span>KYC et AML</span></Link>
        <nav aria-label="Navigation">
          {GROUPS.map((group) => (
            <div key={group.id}>
              <div className="nav-group">{group.label}</div>
              <div className="submenu" aria-label={group.label}>
                {group.links.map(([href, label]) => (
                  <Link key={href} href={href} className={isCurrent(pathname, href, String(profile?.kind || "")) ? "active" : ""}>
                    {label}
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <button
          className="ghost nav-exit"
          onClick={() => {
            sessionStorage.removeItem("clarity_token");
            setToken(null);
          }}
        >
          Quitter
        </button>
      </aside>
      <main className="main">
        <div className="banner">Données de démonstration synthétiques. Aucun criblage ni aucune vérification d'identité de production n'est connecté.</div>
        {error && <p className="error">{error}</p>}
        {customerId && profile ? <CustomerView profile={profile} onChange={setProfile} /> : null}
        {!customerId && (section === "overview" || section === "kyb" || section === "aml") ? <Overview scope={section === "overview" ? "kyc" : section} data={data as Record<string, unknown> | null} /> : null}
        {!customerId && (section === "customers" || section === "businesses") ? (
          <CustomerList title={section === "businesses" ? "Entreprises" : "Clients"} kind={section === "businesses" ? "business" : "individual"} />
        ) : null}
        {!customerId && section === "kyc-sessions" ? <SessionBoard kind="individual" /> : null}
        {!customerId && section === "kyb-sessions" ? <SessionBoard kind="business" /> : null}
        {!customerId && section === "verification" ? <KycDesk /> : null}
        {!customerId && section === "screening" ? <ScreeningDesk /> : null}
        {!customerId && section === "risk" ? <RiskBoard /> : null}
        {!customerId && section === "transactions" ? <TransactionBoard /> : null}
        {!customerId && section === "alerts" ? <AlertBoard /> : null}
        {!customerId && section === "investigations" ? <CaseDesk /> : null}
        {!customerId && section === "reviews" ? <ReviewBoard /> : null}
        {!customerId && section === "reports" ? <ReportBoard /> : null}
        {!customerId && section === "rules" ? <StudioDesk /> : null}
        {!customerId && section === "workflows" ? <WorkflowEditor /> : null}
        {!customerId && section === "audit" ? <AuditBoard /> : null}
        {!customerId && section === "integrations" ? <IntegrationBoard /> : null}
        {!customerId && section === "administration" ? <AdminBoard /> : null}
      </main>
    </div>
  );
}

const OVERVIEWS: Record<string, { title: string; lede: string; cards: Array<[string, string, string]> }> = {
  kyc: {
    title: "KYC",
    lede: "Personnes physiques. L'entrée en relation est ouverte par l'API, puis suivie ici.",
    cards: [
      ["Clients", "individuals", "/customers"],
      ["Sessions", "kyc_sessions", "/kyc-sessions"],
      ["En revue", "individuals_in_review", "/verification"],
    ],
  },
  kyb: {
    title: "KYB",
    lede: "Personnes morales. Une société n'apparaît ici que si l'API a ouvert son dossier.",
    cards: [
      ["Clients", "businesses", "/businesses"],
      ["Sessions", "kyb_sessions", "/kyb-sessions"],
      ["En revue", "businesses_in_review", "/kyb-sessions"],
    ],
  },
  aml: {
    title: "AML",
    lede: "Surveillance, alertes et investigations. Les transactions ne font pas partie du KYC.",
    cards: [
      ["Transactions", "transactions", "/transactions"],
      ["Alertes ouvertes", "open_alerts", "/alerts"],
      ["Investigations", "open_cases", "/investigations"],
      ["Tâches", "open_tasks", "/reviews"],
    ],
  },
};

function Overview({ scope, data }: { scope: string; data: Record<string, unknown> | null }) {
  const view = OVERVIEWS[scope] || OVERVIEWS.kyc;
  if (!data) return <p>Chargement…</p>;
  return (
    <>
      <h1>{view.title}</h1>
      <p className="lede">{view.lede}</p>
      <div className="grid">
        {view.cards.map(([label, key, href]) => (
          <Link className="card" href={href} key={label}>
            <div className="label">{label}</div>
            <div className="metric">{String(data[key] ?? "—")}</div>
          </Link>
        ))}
      </div>
    </>
  );
}

function day(value?: string | null) {
  return value ? value.slice(0, 10) : "—";
}

function CustomerList({ title, kind }: { title: string; kind: string }) {
  const [rows, setRows] = useState<Customer[]>([]);
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");
  const [risk, setRisk] = useState("");
  const [sort, setSort] = useState("display_name");
  const [direction, setDirection] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  useEffect(() => {
    const params = new URLSearchParams({
      kind,
      name,
      status,
      risk,
      sort,
      direction,
      limit: "25",
      offset: String(page * 25),
    });
    void api<{ items: Customer[]; total: number }>(`/api/clarity/v1/customers?${params}`).then((body) => {
      setRows(body.items);
      setTotal(body.total);
    });
  }, [kind, name, status, risk, sort, direction, page]);
  function toggle(column: string) {
    setPage(0);
    if (sort === column) setDirection((current) => (current === "asc" ? "desc" : "asc"));
    else {
      setSort(column);
      setDirection("asc");
    }
  }
  const mark = (column: string) => (sort === column ? (direction === "asc" ? " ↑" : " ↓") : "");
  return (
    <>
      <h1>{title}</h1>
      <p className="lede">Le filtre et le tri sont dans l'en-tête. Overdue reste dans la file pending.</p>
      <table className="inbox sheet">
        <thead>
          <tr>
            <th>
              <button className="sort" type="button" onClick={() => toggle("display_name")}>Client{mark("display_name")}</button>
              <input aria-label="Filtrer par nom" value={name} placeholder="Nom" onChange={(event) => { setPage(0); setName(event.target.value); }} />
            </th>
            <th>
              <button className="sort" type="button" onClick={() => toggle("kyc_label")}>Vérification{mark("kyc_label")}</button>
              <select aria-label="Filtrer par statut" value={status} onChange={(event) => { setPage(0); setStatus(event.target.value); }}>
                <option value="">Tous</option>
                <option value="pending">pending</option>
                <option value="valid">valid</option>
                <option value="expired">expired</option>
                <option value="overdue">overdue</option>
              </select>
            </th>
            <th>
              <button className="sort" type="button" onClick={() => toggle("risk_tier")}>Risque{mark("risk_tier")}</button>
              <select aria-label="Filtrer par risque" value={risk} onChange={(event) => { setPage(0); setRisk(event.target.value); }}>
                <option value="">Tous</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
            </th>
            <th><button className="sort" type="button" onClick={() => toggle("kyc_created_at")}>Création{mark("kyc_created_at")}</button></th>
            <th><button className="sort" type="button" onClick={() => toggle("kyc_validated_at")}>Validation{mark("kyc_validated_at")}</button></th>
            <th><button className="sort" type="button" onClick={() => toggle("kyc_expires_at")}>Expiration{mark("kyc_expires_at")}</button></th>
            <th>Relance</th>
          </tr>
        </thead>
        <tbody>
          {!rows.length ? <tr><td colSpan={7}>Aucun client de ce type.</td></tr> : null}
          {rows.map((row) => (
            <tr key={row.id}>
              <td><Link href={`/customers/${row.id}`}>{row.display_name}</Link><div className="label">{row.kind === "business" ? "Personne morale" : "Personne physique"}</div></td>
              <td><span className={`badge ${row.kyc_status || ""}`}>{row.kyc_label || "—"}</span></td>
              <td>{row.risk_tier || "—"}</td>
              <td>{day(row.kyc_created_at)}</td>
              <td>{day(row.kyc_validated_at)}</td>
              <td>{day(row.kyc_expires_at)}</td>
              <td>{row.reminder && row.reminder !== "none" ? `${row.reminder} · ${day(row.reminded_at)}` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="label">{total} client(s) · page {page + 1}</p>
      <div className="tabs">
        <button className="ghost" type="button" disabled={page === 0} onClick={() => setPage((current) => Math.max(0, current - 1))}>Précédent</button>
        <button className="ghost" type="button" disabled={(page + 1) * 25 >= total} onClick={() => setPage((current) => current + 1)}>Suivant</button>
      </div>
    </>
  );
}

function CustomerView({ profile, onChange }: { profile: Record<string, unknown>; onChange: (next: Record<string, unknown>) => void }) {
  const checks = (profile.verification_checks as Array<Record<string, unknown>>) || [];
  const cycles = (profile.kyc_cycles as KycCycle[]) || [];
  const screening = (profile.screening as Array<Record<string, unknown>>) || [];
  const risk = profile.risk as Record<string, unknown> | null;
  async function remind(cycleId: string) {
    await api(`/api/clarity/v1/customers/${profile.id}/verifications/${cycleId}/remind`, { method: "POST" });
    onChange(await api(`/api/clarity/v1/customers/${profile.id}`));
  }
  return (
    <>
      <h1>{String(profile.display_name)}</h1>
      <p className="lede">Statut {String(profile.status)} · risque {String(profile.risk_tier || "non calculé")} · référence {String(profile.external_ref || "—")}</p>
      <h2>Vérifications</h2>
      <table className="inbox sheet">
        <thead>
          <tr><th>Statut</th><th>Création</th><th>Validation</th><th>Expiration</th><th>Relance</th><th></th></tr>
        </thead>
        <tbody>
          {!cycles.length ? <tr><td colSpan={6}>Aucune vérification.</td></tr> : null}
          {cycles.map((cycle) => (
            <tr key={cycle.id}>
              <td><span className={`badge ${cycle.status}`}>{cycle.label}</span></td>
              <td>{day(cycle.created_at)}</td>
              <td>{day(cycle.validated_at)}</td>
              <td>{day(cycle.expires_at)}</td>
              <td>{cycle.reminder === "none" ? "—" : `${cycle.reminder} · ${day(cycle.reminded_at)}`}</td>
              <td>{cycle.status === "valid" ? null : <button type="button" onClick={() => void remind(cycle.id)}>Relancer</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="label">À l'expiration, une relance automatique est enregistrée. Trente jours plus tard, overdue s'ajoute à pending. La relance manuelle reste disponible.</p>
      <div className="split">
        <section>
          <h2>Contrôles</h2>
          {checks.map((check) => (
            <p className="factor" key={String(check.id)}>
              {String(check.provider)} · production {String(check.production)} · {String((check.detail as Record<string, unknown>)?.notice || check.status)}
            </p>
          ))}
          <h2>Criblage</h2>
          {screening.flatMap((item) => ((item.matches as Array<Record<string, unknown>>) || []).map((match) => (
            <p className="factor" key={String(match.id)}>
              {String(match.status)} · score {String(match.score)} · {String(match.entity_id)} · synthétique {String(match.synthetic)}
            </p>
          )))}
        </section>
        <section className="card">
          <h2>Score</h2>
          {risk ? (
            <>
              <div className="metric">{String(risk.score)}</div>
              <p>{String(risk.tier)} · {String(risk.methodology_version)} · source {String(risk.source)}</p>
              {((risk.factors as Array<Record<string, unknown>>) || []).map((factor, index) => (
                <p className="factor" key={index}>{String(factor.points)} pts — {String(factor.explanation)}</p>
              ))}
            </>
          ) : <p>Pas encore de score.</p>}
        </section>
      </div>
    </>
  );
}

type SessionRow = {
  id: string;
  customer_id: string | null;
  customer_name: string | null;
  definition: string;
  status: string;
  state: string | null;
  task: string | null;
  engine: string;
  updated_at: string;
  ballerine_runtime_id: string;
};

const STEP_LABEL: Record<string, string> = {
  collect: "Collecte",
  verify: "Vérification",
  screen: "Criblage",
  score: "Évaluation du risque",
  analystReview: "Revue analyste",
  checkerReview: "Contrôle checker",
  remediate: "Complément",
  investigate: "Investigation",
  approved: "Approuvé",
  rejected: "Rejeté",
  closed: "Clôturé",
};

function stepLabel(state: string | null | undefined, fallback: string) {
  if (!state) return fallback;
  return STEP_LABEL[state] || state;
}

function SessionBoard({ kind }: { kind: "individual" | "business" }) {
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [selected, setSelected] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const title = kind === "business" ? "Sessions KYB" : "Sessions KYC";
  useEffect(() => {
    void searchPage<SessionRow>(`/api/clarity/v1/workflows?kind=${kind}`, query, page).then((body) => {
      setRows(body.items);
      setTotal(body.total);
      setSelected((current) => (body.items.some((row) => row.id === current) ? current : body.items[0]?.id || ""));
    });
  }, [kind, query, page]);
  const current = rows.find((row) => row.id === selected) || rows[0];
  return (
    <>
      <h1>{title}</h1>
      <p className="lede">File des parcours déjà ouverts. L'API d'entrée en relation crée la session. Cet écran la suit, il ne la démarre pas.</p>
      <QueryStack query={query} onChange={(next) => { setPage(0); setQuery(next); }} fields={[{ key: "status", label: "Statut", options: ["active", "completed", "failed"] }, { key: "state", label: "Étape" }, { key: "name", label: "Client" }]} total={total} page={page} pageSize={25} onPage={setPage} />
      <div className="split">
        <table className="inbox">
          <thead><tr><th>Client</th><th>Étape</th><th>Statut</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className={row.id === current?.id ? "selected" : ""} onClick={() => setSelected(row.id)}>
                <td>{row.customer_name || "—"}</td>
                <td>{stepLabel(row.state, row.task || "—")}</td>
                <td><span className={`badge ${row.status}`}>{row.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        <section className="card">
          {current ? (
            <>
              <h2>{current.customer_name || "Session"}</h2>
              <dl className="kv">
                <dt>Parcours</dt><dd>{current.definition}</dd>
                <dt>Étape</dt><dd>{stepLabel(current.state, "—")}</dd>
                <dt>Tâche</dt><dd>{current.task || "aucune tâche ouverte"}</dd>
                <dt>Moteur</dt><dd>{current.engine}</dd>
                <dt>Référence</dt><dd>{current.ballerine_runtime_id}</dd>
                <dt>Mise à jour</dt><dd>{current.updated_at}</dd>
              </dl>
              {current.customer_id ? <Link className="tool-link wide-link" href={`/customers/${current.customer_id}`}>Ouvrir la fiche</Link> : null}
            </>
          ) : <p>Aucune session de ce type.</p>}
        </section>
      </div>
    </>
  );
}

