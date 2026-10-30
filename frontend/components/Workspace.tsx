"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import CaseDesk from "./CaseDesk";
import KycDesk, { RiskScoreChip, VerificationPreview } from "./KycDesk";
import ScreeningDesk from "./ScreeningDesk";
import StudioDesk from "./StudioDesk";
import WorkflowEditor from "./WorkflowEditor";
import { AdminBoard, AlertBoard, AuditBoard, IntegrationBoard, ReportBoard, ReviewBoard, RiskBoard, TransactionBoard } from "./Workboards";
import { QueryStack } from "./QueryStack";
import { api, searchPage } from "../lib/api";
import { logout as keycloakLogout, startKeycloak } from "../lib/keycloak";
import { LOCALES, useI18n } from "../lib/i18n";

const GROUPS = [
  {
    id: "kyc",
    labelKey: "nav.kyc",
    links: [
      ["/", "nav.overview"],
      ["/customers", "nav.customers"],
      ["/kyc-sessions", "nav.sessions"],
      ["/verification", "nav.verification"],
    ],
  },
  {
    id: "kyb",
    labelKey: "nav.kyb",
    links: [
      ["/kyb", "nav.overview"],
      ["/businesses", "nav.businesses"],
      ["/kyb-sessions", "nav.sessions"],
      ["/kyb-verification", "nav.verification"],
    ],
  },
  {
    id: "aml",
    labelKey: "nav.aml",
    links: [
      ["/aml", "nav.overview"],
      ["/screening", "nav.screening"],
      ["/risk", "nav.risk"],
      ["/transactions", "nav.transactions"],
      ["/alerts", "nav.alerts"],
      ["/investigations", "nav.investigations"],
      ["/reviews", "nav.reviews"],
      ["/reports", "nav.reports"],
    ],
  },
  {
    id: "settings",
    labelKey: "nav.settings",
    links: [
      ["/rules", "nav.rules"],
      ["/workflows", "nav.workflows"],
      ["/audit", "nav.audit"],
      ["/integrations", "nav.integrations"],
      ["/administration", "nav.administration"],
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

type CycleDocument = { id: string; title: string; decision?: string | null; url: string };

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
  workflow_runtime_id?: string | null;
  documents?: CycleDocument[];
};

const CLIENT_STATUSES = [
  { value: "pending", label: "En attente" },
  { value: "valid", label: "Valide" },
  { value: "expired", label: "Expiré" },
  { value: "overdue", label: "Relance" },
  { value: "approved-partially", label: "Approuvé partiellement" },
  { value: "rejected", label: "Rejeté" },
];

const CLIENT_RISKS = [
  { value: "low", label: "Faible" },
  { value: "medium", label: "Moyen" },
  { value: "high", label: "Élevé" },
];


export default function Workspace({ section, customerId }: { section: string; customerId?: string }) {
  const pathname = usePathname();
  const { t, locale, setLocale } = useI18n();
  const [token, setToken] = useState<string | null>(null);
  const [authMode, setAuthMode] = useState<"" | "oidc" | "password">("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [account, setAccount] = useState<{ name?: string; role?: string; roles?: string[] } | null>(null);
  const [error, setError] = useState("");
  const [data, setData] = useState<unknown>(null);
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api<{ mode: string }>("/api/clarity/v1/auth/mode")
      .then(async (body) => {
        if (cancelled) return;
        const mode = body.mode === "password" ? "password" : "oidc";
        setAuthMode(mode);
        if (mode === "password") {
          setToken(sessionStorage.getItem("clarity_token"));
          return;
        }
        const client = await startKeycloak();
        if (cancelled) return;
        if (client.token) {
          sessionStorage.setItem("clarity_token", client.token);
          setToken(client.token);
        } else {
          sessionStorage.removeItem("clarity_token");
          setToken(null);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setError("L'authentification est indisponible.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!token) {
      setAccount(null);
      return;
    }
    void api<{ name: string; role: string }>("/api/clarity/v1/me")
      .then(setAccount)
      .catch((err: Error) => {
        sessionStorage.removeItem("clarity_token");
        setToken(null);
        setAccount(null);
        setError(err.message);
      });
  }, [token]);

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

  function quit() {
    sessionStorage.removeItem("clarity_token");
    setToken(null);
    setAccount(null);
    if (authMode === "password") return;
    void keycloakLogout();
  }

  async function enter(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const body = await api<{ access_token: string }>("/api/clarity/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      sessionStorage.setItem("clarity_token", body.access_token);
      setPassword("");
      setToken(body.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Identifiants invalides.");
    }
  }

  if (!token && authMode === "password") {
    return (
      <section className="login">
        <img className="mark" src="/logo.svg" alt="" width={72} height={72} />
        <div className="brand">ClarityCheck<span>KYC · KYB · AML · Conformité</span></div>
        <form onSubmit={(event) => void enter(event)}>
          <label>
            Identifiant
            <input value={email} autoComplete="username" onChange={(event) => setEmail(event.target.value)} required />
          </label>
          <label>
            Mot de passe
            <input type="password" value={password} autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} required />
          </label>
          <button type="submit">Entrer</button>
        </form>
        {error ? <p className="error">{error}</p> : null}
      </section>
    );
  }

  if (!token) {
    return error ? <p className="error">{error}</p> : null;
  }

  if (!account) {
    return (
      <section className="login">
        <p className="lede">Chargement de la session…</p>
        {error ? <p className="error">{error}</p> : null}
      </section>
    );
  }

  if (account.role === "pending" || (account.roles?.length === 1 && account.roles[0] === "pending")) {
    return (
      <section className="login">
        <img className="mark" src="/logo.svg" alt="" width={72} height={72} />
        <div className="brand">ClarityCheck<span>KYC · KYB · AML · Conformité</span></div>
        <p className="lede">Le compte {account.name} est créé. Un administrateur doit lui attribuer un rôle.</p>
        <button type="button" onClick={quit}>Quitter</button>
      </section>
    );
  }

  return (
    <div className="shell">
      <aside className="nav">
        <Link href="/" className="brand"><img src="/logo.svg" alt="" width={28} height={28} /><span className="title">ClarityCheck<span>KYC · KYB · AML</span></span></Link>
        <nav aria-label="Navigation">
          {GROUPS.map((group) => (
            <div key={group.id}>
              <div className="nav-group">{t(group.labelKey)}</div>
              <div className="submenu" aria-label={t(group.labelKey)}>
                {group.links.map(([href, labelKey]) => (
                  <Link key={href} href={href} className={isCurrent(pathname, href, String(profile?.kind || "")) ? "active" : ""}>
                    {t(labelKey)}
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <button
          className="ghost nav-exit"
          onClick={quit}
        >
          {t("nav.quit")}
        </button>
      </aside>
      <main className="main">
        <div className="main-topbar">
          <div className="lang-switch" role="group" aria-label={t("lang.label")}>
            {LOCALES.map((item) => (
              <button
                key={item.id}
                type="button"
                className={locale === item.id ? "is-active" : ""}
                aria-pressed={locale === item.id}
                onClick={() => setLocale(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {customerId && profile ? <CustomerView profile={profile} onChange={setProfile} /> : null}
        {!customerId && (section === "overview" || section === "kyb" || section === "aml") ? <Overview scope={section === "overview" ? "kyc" : section} data={data as Record<string, unknown> | null} /> : null}
        {!customerId && (section === "customers" || section === "businesses") ? (
          <CustomerList title={section === "businesses" ? "Entreprises" : "Clients"} kind={section === "businesses" ? "business" : "individual"} />
        ) : null}
        {!customerId && section === "kyc-sessions" ? <SessionBoard kind="individual" /> : null}
        {!customerId && section === "kyb-sessions" ? <SessionBoard kind="business" /> : null}
        {!customerId && section === "verification" ? <KycDesk /> : null}
        {!customerId && section === "kyb-verification" ? <KycDesk kind="business" /> : null}
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
      ["En revue", "businesses_in_review", "/kyb-verification"],
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
  const [statuses, setStatuses] = useState<string[]>([]);
  const [risks, setRisks] = useState<string[]>([]);
  const [sort, setSort] = useState("display_name");
  const [direction, setDirection] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  useEffect(() => {
    const params = new URLSearchParams({
      kind,
      name,
      status: statuses.join(","),
      risk: risks.join(","),
      sort,
      direction,
      limit: "25",
      offset: String(page * 25),
    });
    void api<{ items: Customer[]; total: number }>(`/api/clarity/v1/customers?${params}`).then((body) => {
      setRows(body.items);
      setTotal(body.total);
    });
  }, [kind, name, statuses, risks, sort, direction, page]);
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
      <p className="lede">Filtrez par nom, statut de vérification ou niveau de risque. Cliquez une colonne pour trier.</p>
      <div className="filters">
        <div className="filter-group">
          <span className="label">Nom</span>
          <input aria-label="Filtrer par nom" placeholder="Rechercher" value={name} onChange={(event) => { setPage(0); setName(event.target.value); }} />
        </div>
        <div className="filter-group">
          <span className="label">Statut de la vérification</span>
          <div className="checks" role="group" aria-label="Statut de la vérification">
            {CLIENT_STATUSES.map((item) => (
              <label key={item.value}>
                <input
                  type="checkbox"
                  checked={statuses.includes(item.value)}
                  onChange={() => {
                    setPage(0);
                    setStatuses((current) => current.includes(item.value) ? current.filter((value) => value !== item.value) : [...current, item.value]);
                  }}
                />
                {item.label}
              </label>
            ))}
          </div>
        </div>
        <div className="filter-group">
          <span className="label">Niveau de risque</span>
          <div className="checks" role="group" aria-label="Niveau de risque">
            {CLIENT_RISKS.map((item) => (
              <label key={item.value}>
                <input
                  type="checkbox"
                  checked={risks.includes(item.value)}
                  onChange={() => {
                    setPage(0);
                    setRisks((current) => current.includes(item.value) ? current.filter((value) => value !== item.value) : [...current, item.value]);
                  }}
                />
                {item.label}
              </label>
            ))}
          </div>
        </div>
      </div>
      <table className="inbox sheet">
        <thead>
          <tr>
            <th><button className="sort" type="button" onClick={() => toggle("display_name")}>Client{mark("display_name")}</button></th>
            <th><button className="sort" type="button" onClick={() => toggle("kyc_label")}>Vérification{mark("kyc_label")}</button></th>
            <th><button className="sort" type="button" onClick={() => toggle("risk_tier")}>Risque{mark("risk_tier")}</button></th>
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
  const cycles = (profile.kyc_cycles as KycCycle[]) || [];
  const risk = (profile.risk as { score?: number | null; tier?: string | null; formula_note?: string; factors?: Array<{ code: string; points: number; explanation: string }> } | null) || null;
  const latestCycle = cycles[0];
  const [openId, setOpenId] = useState(cycles[0]?.id || "");
  useEffect(() => {
    if (!cycles.some((cycle) => cycle.id === openId)) setOpenId(cycles[0]?.id || "");
  }, [cycles, openId]);
  const openCycle = cycles.find((cycle) => cycle.id === openId) || cycles[0];
  async function refreshProfile() {
    onChange(await api(`/api/clarity/v1/customers/${profile.id}`));
  }
  return (
    <>
      <header className="customer-fiche-head">
        <div>
          <h1>{String(profile.display_name)}</h1>
          <p className="lede">
            Statut {String(profile.status)}
            {latestCycle ? ` · dernière vérification ${latestCycle.label}` : ""}
            {" · "}référence {String(profile.external_ref || "—")}
          </p>
        </div>
        <RiskScoreChip
          score={risk?.score}
          tier={risk?.tier || (typeof profile.risk_tier === "string" ? profile.risk_tier : null)}
          note={risk?.formula_note}
          factors={risk?.factors || []}
        />
      </header>
      <h2>Vérifications</h2>
      <table className="inbox sheet">
        <thead>
          <tr><th>Statut</th><th>Date</th><th>Validation</th><th>Expiration</th><th>Relance</th></tr>
        </thead>
        <tbody>
          {!cycles.length ? <tr><td colSpan={5}>Aucune vérification.</td></tr> : null}
          {cycles.map((cycle) => (
            <tr key={cycle.id} className={cycle.id === openId ? "selected" : ""} onClick={() => setOpenId(cycle.id)}>
              <td><span className={`badge ${cycle.status}`}>{cycle.label}</span></td>
              <td>{day(cycle.created_at)}</td>
              <td>{day(cycle.validated_at)}</td>
              <td>{day(cycle.expires_at)}</td>
              <td>{cycle.reminder === "none" ? "—" : `${cycle.reminder} · ${day(cycle.reminded_at)}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {openCycle ? (
        <section className="customer-verification-detail" aria-label="Détail de la vérification">
          <h3>{openCycle.label} · {day(openCycle.created_at)}</h3>
          {openCycle.workflow_runtime_id ? (
            <VerificationPreview runtimeId={openCycle.workflow_runtime_id} onChanged={() => void refreshProfile()} />
          ) : (
            <p className="label">Aucun dossier ouvert pour cette vérification. Ouvrez le bureau Vérification pour instruire le dossier.</p>
          )}
        </section>
      ) : null}
      <p className="label">Le score affiché correspond à la dernière vérification. Les notes sont dans l&apos;onglet Notes. Les pièces se demandent dans l&apos;onglet Action.</p>
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

