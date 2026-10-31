"use client";

import { FormEvent, useEffect, useState } from "react";
import { QueryStack, queryMatches } from "./QueryStack";
import { api } from "../lib/api";
import { Copy, copyFrom, preferred } from "./LanguageFields";
import { useI18n } from "../lib/i18n";

type Scenario = { id: string; name: string; description?: string; trigger_object_type: string; live_version_id?: string; i18n?: { name?: Partial<Copy>; description?: Partial<Copy> } };
type Iteration = { id: string; version: number | null; body: { score_review_threshold?: number | null; score_block_and_review_threshold?: number | null; score_decline_threshold?: number | null; rules?: string[] } };
type Rule = { id: string; name: string; description: string; score_modifier: number; formula_ast_expression?: { name?: string } };

const PANES = ["Déclencheur", "Règles", "Décision"] as const;

function comparison(operator: string, field: string, value: string) {
  const numeric = Number(value);
  const constant = value !== "" && Number.isFinite(numeric) ? numeric : value;
  return { name: operator, children: [{ name: "Payload", named_children: { path: { constant: field } } }, { constant }] };
}

export default function ScenarioEditor() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selected, setSelected] = useState("");
  const [iteration, setIteration] = useState<Iteration | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [pane, setPane] = useState<(typeof PANES)[number]>("Règles");
  const [name, setName] = useState("Montant élevé");
  const [field, setField] = useState("amount");
  const [operator, setOperator] = useState(">=");
  const [value, setValue] = useState("10000");
  const [score, setScore] = useState("40");
  const [review, setReview] = useState("30");
  const [block, setBlock] = useState("70");
  const [decline, setDecline] = useState("120");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [listPage, setListPage] = useState(0);
  const { locale } = useI18n();
  const [names, setNames] = useState<Copy>(copyFrom(undefined));
  const [descriptions, setDescriptions] = useState<Copy>(copyFrom(undefined));
  const live = Boolean(iteration?.version);

  async function loadScenarios(prefer?: string) {
    const rows = await api<Scenario[]>("/scenarios");
    setScenarios(rows);
    const id = prefer && rows.some((row) => row.id === prefer) ? prefer : rows[0]?.id || "";
    setSelected(id);
    const row = rows.find((item) => item.id === id);
    setNames(copyFrom(row?.i18n?.name, row?.name || ""));
    setDescriptions(copyFrom(row?.i18n?.description, row?.description || ""));
  }

  async function loadIteration(scenarioId: string) {
    const rows = await api<Iteration[]>(`/scenario-iterations?scenario_id=${scenarioId}`);
    const draft = rows.find((row) => row.version == null) || rows[rows.length - 1];
    setIteration(draft || null);
    if (draft?.body) {
      setReview(String(draft.body.score_review_threshold ?? ""));
      setBlock(String(draft.body.score_block_and_review_threshold ?? ""));
      setDecline(String(draft.body.score_decline_threshold ?? ""));
    }
    setRules(draft ? await api<Rule[]>(`/scenario-iteration-rules?scenario_iteration_id=${draft.id}`) : []);
  }

  useEffect(() => {
    void loadScenarios()
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected) return;
    const row = scenarios.find((item) => item.id === selected);
    if (row) {
      setNames(copyFrom(row.i18n?.name, row.name));
      setDescriptions(copyFrom(row.i18n?.description, row.description || ""));
    }
    void loadIteration(selected).catch((err: Error) => setError(err.message));
  }, [selected]);

  async function createScenario(event: FormEvent) {
    event.preventDefault();
    const created = await api<Scenario>("/scenarios", {
      method: "POST",
      body: JSON.stringify({ name, description: "Scénario créé depuis l'atelier.", trigger_object_type: "transactions" }),
    });
    setMessage("Brouillon créé. Ajoutez des règles, puis fixez les seuils et publiez.");
    await loadScenarios(created.id);
  }

  async function draftFromLive() {
    if (!iteration) return;
    const created = await api<Iteration>("/scenario-iterations", {
      method: "POST",
      body: JSON.stringify({ scenario_id: selected, body: iteration.body }),
    });
    for (const rule of rules) {
      await api("/scenario-iteration-rules", {
        method: "POST",
        body: JSON.stringify({
          scenario_iteration_id: created.id,
          display_order: rules.indexOf(rule) + 1,
          name: rule.name,
          description: rule.description || rule.name,
          rule_group: "atelier",
          score_modifier: rule.score_modifier,
          formula_ast_expression: rule.formula_ast_expression,
        }),
      });
    }
    setMessage("Brouillon ouvert à partir de la version publiée.");
    await loadIteration(selected);
  }

  async function addRule(event: FormEvent) {
    event.preventDefault();
    if (!iteration || live) return;
    const formula = operator === "Screen" ? { name: "Screen" } : comparison(operator, field, value);
    await api("/scenario-iteration-rules", {
      method: "POST",
      body: JSON.stringify({
        scenario_iteration_id: iteration.id,
        display_order: rules.length + 1,
        name: operator === "Screen" ? "Criblage de la contrepartie" : `${field} ${operator} ${value}`,
        description: "Règle d'atelier. Un criblage reste une correspondance candidate.",
        rule_group: "atelier",
        score_modifier: Number(score),
        formula_ast_expression: formula,
      }),
    });
    setMessage("Règle ajoutée au brouillon.");
    await loadIteration(selected);
  }

  async function saveThresholds(event: FormEvent) {
    event.preventDefault();
    if (!iteration || live) return;
    await api(`/scenario-iterations/${iteration.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        body: {
          score_review_threshold: Number(review),
          score_block_and_review_threshold: Number(block),
          score_decline_threshold: Number(decline),
        },
      }),
    });
    setMessage("Seuils enregistrés. Revue ≤ blocage ≤ refus.");
    await loadIteration(selected);
  }

  async function saveCopy(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    setError("");
    try {
      const saved = await api<Scenario>(`/scenarios/${selected}`, {
        method: "PATCH",
        body: JSON.stringify({ i18n: { name: names, description: descriptions } }),
      });
      setScenarios((rows) => rows.map((row) => (row.id === saved.id ? saved : row)));
      setMessage("Libellés enregistrés pour le français, l'anglais et l'arabe.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function publish() {
    if (!iteration) return;
    setError("");
    try {
      const result = await api<{ status: string }>(`/scenario-iterations/${iteration.id}/commit`, { method: "POST", body: "{}" });
      setMessage(`Version publiée (${result.status}). Les prochaines décisions utilisent ces règles.`);
      await loadScenarios(selected);
      await loadIteration(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Publication refusée");
    }
  }

  const current = scenarios.find((row) => row.id === selected);
  const visibleScenarios = scenarios.filter((row) => queryMatches(query, { name: row.name, trigger: row.trigger_object_type, status: row.live_version_id ? "publié" : "brouillon" }));
  const scenarioPage = visibleScenarios.slice(listPage * 12, listPage * 12 + 12);

  return (
    <div className="editor">
      <div className="queue">
        <form className="form" onSubmit={createScenario}>
          <input className="wide" aria-label="Nom du scénario" value={name} onChange={(event) => setName(event.target.value)} />
          <button type="submit">Créer</button>
        </form>
        <QueryStack
          query={query}
          onChange={(next) => { setListPage(0); setQuery(next); }}
          fields={[{ key: "trigger", label: "Déclencheur", options: ["transactions", "clients", "prospects"] }, { key: "status", label: "État", options: ["publié", "brouillon"] }, { key: "name", label: "Nom" }]}
          total={visibleScenarios.length}
          page={listPage}
          pageSize={12}
          onPage={setListPage}
        />
        {scenarioPage.map((row) => (
          <button className={row.id === selected ? "active" : "ghost"} key={row.id} type="button" onClick={() => setSelected(row.id)}>
            <span>{row.i18n?.name?.[locale] || row.name}</span>
            <span className="label">{row.trigger_object_type} · {row.live_version_id ? "publié" : "brouillon"}</span>
          </button>
        ))}
      </div>
      {current && iteration ? (
        <section className="case-sheet">
          <header className="case-bar">
            <div>
              <h2>{names[locale] || preferred(names, current.name)}</h2>
              <p className="label">{live ? `Version ${iteration.version} publiée` : "Brouillon"} · déclencheur {current.trigger_object_type}</p>
            </div>
            {live ? <button type="button" onClick={() => void draftFromLive()}>Nouveau brouillon</button> : <button type="button" onClick={() => void publish()}>Publier</button>}
          </header>
          {error ? <p className="error">{error}</p> : null}
          {message ? <p className="label">{message}</p> : null}
          <form className="card connector" onSubmit={(event) => void saveCopy(event)}>
            <label>
              Nom
              <input dir={locale === "ar" ? "rtl" : "ltr"} value={names[locale]} onChange={(event) => setNames((current) => ({ ...current, [locale]: event.target.value }))} />
            </label>
            <label>
              Description
              <input dir={locale === "ar" ? "rtl" : "ltr"} value={descriptions[locale]} onChange={(event) => setDescriptions((current) => ({ ...current, [locale]: event.target.value }))} />
            </label>
            <button type="submit">Enregistrer les libellés</button>
          </form>
          <div className="tabs">
            {PANES.map((item) => <button className={pane === item ? "active" : ""} key={item} type="button" onClick={() => setPane(item)}>{item}</button>)}
          </div>
          {pane === "Déclencheur" ? (
            <article className="card">
              <dl className="kv">
                <dt>Objet</dt><dd>{current.trigger_object_type}</dd>
                <dt>Description</dt><dd>{current.description || "Surveillance configurée dans l'atelier."}</dd>
              </dl>
              <p className="label">L'objet déclencheur vient du modèle de données. Une itération publiée ne se modifie pas : ouvrez un brouillon.</p>
            </article>
          ) : null}
          {pane === "Règles" ? (
            <>
              {live ? null : (
                <form className="form" onSubmit={addRule}>
                  <select aria-label="Opérateur" value={operator} onChange={(event) => setOperator(event.target.value)}>
                    <option value=">=">&gt;=</option>
                    <option value=">">&gt;</option>
                    <option value="=">=</option>
                    <option value="!=">≠</option>
                    <option value="Screen">Criblage</option>
                  </select>
                  <input aria-label="Champ" value={field} onChange={(event) => setField(event.target.value)} disabled={operator === "Screen"} />
                  <input aria-label="Valeur" value={value} onChange={(event) => setValue(event.target.value)} disabled={operator === "Screen"} />
                  <input aria-label="Score" value={score} onChange={(event) => setScore(event.target.value)} />
                  <button type="submit">Ajouter la règle</button>
                </form>
              )}
              <div className="list">
                {rules.map((rule) => (
                  <div className="row" key={rule.id}>
                    <span>{rule.name}<div className="label">{rule.formula_ast_expression?.name || "formule"} · {rule.description}</div></span>
                    <span className="tag">+{rule.score_modifier}</span>
                  </div>
                ))}
                {!rules.length ? <p className="label">Aucune règle dans cette itération.</p> : null}
              </div>
            </>
          ) : null}
          {pane === "Décision" ? (
            <form className="form" onSubmit={saveThresholds}>
              <label>Revue<input aria-label="Seuil de revue" value={review} onChange={(event) => setReview(event.target.value)} disabled={live} /></label>
              <label>Blocage et revue<input aria-label="Seuil de blocage" value={block} onChange={(event) => setBlock(event.target.value)} disabled={live} /></label>
              <label>Refus<input aria-label="Seuil de refus" value={decline} onChange={(event) => setDecline(event.target.value)} disabled={live} /></label>
              {live ? <p className="label">Les seuils publiés sont en lecture seule.</p> : <button type="submit">Enregistrer les seuils</button>}
            </form>
          ) : null}
        </section>
      ) : (
        <section className="case-sheet">
          {error ? <p className="error">{error}</p> : null}
          {!error && loading ? <p>Chargement du scénario…</p> : null}
          {!error && !loading && !current ? <p>Aucun scénario.</p> : null}
          {!error && !loading && current && !iteration ? <p>Ce scénario n'a pas encore d'itération.</p> : null}
        </section>
      )}
    </div>
  );
}
