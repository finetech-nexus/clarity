"use client";

import { FormEvent, useEffect, useState } from "react";
import { QueryStack, queryMatches } from "./QueryStack";
import { api } from "../lib/api";
import { Copy, Lang, LanguageTabs, copyFrom, preferred } from "./LanguageFields";

const PROCESSES = [
  ["clarityKycIndividual", "Personne physique"],
  ["clarityKybBusiness", "Personne morale"],
  ["clarityAlertInvestigation", "Investigation d'alerte"],
  ["risk-score-improvement", "Revue du score de risque"],
] as const;

const PROCESS_FLOW: Record<string, Record<string, Record<string, string>>> = {
  clarityKycIndividual: {
    collect: { NEXT: "verify" },
    verify: { NEXT: "screen" },
    screen: { NEXT: "score" },
    score: { NEXT: "analystReview" },
    analystReview: { approve: "checkerReview", reject: "rejected", remediate: "remediate" },
    checkerReview: { approve: "approved", reject: "rejected", return: "analystReview" },
    remediate: { NEXT: "collect" },
  },
  clarityKybBusiness: {
    collect: { NEXT: "verify" },
    verify: { NEXT: "screen" },
    screen: { NEXT: "score" },
    score: { NEXT: "analystReview" },
    analystReview: { approve: "checkerReview", reject: "rejected", remediate: "remediate" },
    checkerReview: { approve: "approved", reject: "rejected", return: "analystReview" },
    remediate: { NEXT: "collect" },
  },
  clarityAlertInvestigation: {
    investigate: { close: "closed", escalate: "checkerReview" },
    checkerReview: { approve: "closed", return: "investigate" },
  },
  "risk-score-improvement": {
    screen: { NEXT: "score" },
    score: { NEXT: "analystReview" },
    analystReview: { approve: "checkerReview", reject: "rejected", remediate: "remediate" },
    checkerReview: { approve: "approved", reject: "rejected", return: "analystReview" },
    remediate: { NEXT: "screen" },
  },
};

const TERMINALS = new Set(["approved", "rejected", "closed"]);

type TransitionValue = string | { target?: string; cond?: unknown } | Array<{ target?: string; cond?: unknown }>;

type StateNode = {
  on?: Record<string, TransitionValue>;
  tags?: string[];
  type?: string;
  meta?: { label?: string; i18n?: Partial<Copy> };
  always?: unknown;
  [key: string]: unknown;
};

type Chart = {
  id?: string;
  initial?: string;
  states?: Record<string, StateNode>;
  [key: string]: unknown;
};

type Definition = {
  id: string;
  name: string;
  displayName?: string;
  version?: number;
  definitionType?: string;
  definition: Chart;
  processKey?: string;
  entityType?: string;
  extensions?: { originalProcessKey?: string; i18n?: { displayName?: Partial<Copy> } };
};

function stepCopy(node: StateNode | undefined): Copy {
  return copyFrom(node?.meta?.i18n, node?.meta?.label || "");
}

type Slot = { event: string; index: number; target: string; cond?: unknown };

type Edge = { from: string; to: string; label: string; virtual: boolean; key: string };

const NODE_W = 168;
const NODE_H = 58;
const COLUMN = 292;

function slotsOf(node: StateNode | undefined): Slot[] {
  const on = node?.on || {};
  const slots: Slot[] = [];
  for (const [event, value] of Object.entries(on)) {
    if (typeof value === "string") slots.push({ event, index: 0, target: value });
    else if (Array.isArray(value)) {
      value.forEach((item, index) => slots.push({ event, index, target: String(item?.target || ""), cond: item?.cond }));
    } else if (value && typeof value === "object") {
      slots.push({ event, index: 0, target: String(value.target || ""), cond: value.cond });
    }
  }
  return slots;
}

function alwaysTargets(node: StateNode | undefined): string[] {
  const always = node?.always;
  if (!always) return [];
  const list = Array.isArray(always) ? always : [always];
  return list
    .map((item) => (typeof item === "string" ? item : (item as { target?: string })?.target))
    .filter((target): target is string => Boolean(target));
}

function condText(cond: unknown): string {
  if (!cond) return "";
  if (typeof cond === "string") return cond;
  if (typeof cond === "object" && cond && "options" in cond) {
    const rule = (cond as { options?: { rule?: string } }).options?.rule;
    if (rule) return rule;
  }
  return JSON.stringify(cond);
}

function patch(chart: Chart, stateId: string, node: StateNode): Chart {
  return { ...chart, states: { ...(chart.states || {}), [stateId]: node } };
}

function rewriteTarget(value: unknown, from: string, to: string): unknown {
  if (typeof value === "string") return value === from ? to : value;
  if (Array.isArray(value)) return value.map((item) => rewriteTarget(item, from, to));
  if (value && typeof value === "object" && "target" in value) {
    const target = (value as { target?: string }).target;
    return { ...value, target: target === from ? to : target };
  }
  return value;
}

function retargetNode(node: StateNode, from: string, to: string): StateNode {
  const next: StateNode = { ...node };
  if (node.on) {
    next.on = Object.fromEntries(Object.entries(node.on).map(([event, value]) => [event, rewriteTarget(value, from, to) as TransitionValue]));
  }
  if (node.always) next.always = rewriteTarget(node.always, from, to);
  return next;
}

function takeSlot(on: Record<string, TransitionValue>, slot: Slot): { on: Record<string, TransitionValue>; value: TransitionValue } {
  const next = { ...on };
  const current = next[slot.event];
  if (typeof current === "string" || (current && !Array.isArray(current))) {
    delete next[slot.event];
    return { on: next, value: current };
  }
  const list = [...current];
  const [item] = list.splice(slot.index, 1);
  if (!list.length) delete next[slot.event];
  else if (list.length === 1 && !list[0]?.cond) next[slot.event] = String(list[0]?.target || "");
  else next[slot.event] = list;
  return { on: next, value: item || { target: "" } };
}

function placeValue(on: Record<string, TransitionValue>, event: string, value: TransitionValue) {
  const existing = on[event];
  if (!existing) {
    on[event] = value;
    return;
  }
  const asObject = (item: TransitionValue) => (typeof item === "string" ? { target: item } : item);
  if (Array.isArray(existing)) on[event] = [...existing, asObject(value) as { target?: string }];
  else on[event] = [asObject(existing) as { target?: string }, asObject(value) as { target?: string }];
}

function storedEdges(chart: Chart): Edge[] {
  const states = chart.states || {};
  const edges: Edge[] = [];
  for (const [from, node] of Object.entries(states)) {
    slotsOf(node).forEach((slot, index) => {
      if (!slot.target) return;
      edges.push({ from, to: slot.target, label: slot.event, virtual: false, key: `${from}-${slot.event}-${index}` });
    });
    alwaysTargets(node).forEach((target, index) => {
      edges.push({ from, to: target, label: "auto", virtual: false, key: `${from}-auto-${index}` });
    });
  }
  return edges;
}

function virtualEdges(processKey: string): Edge[] {
  const flow = PROCESS_FLOW[processKey];
  if (!flow) return [];
  const edges: Edge[] = [];
  for (const [from, routes] of Object.entries(flow)) {
    for (const [event, target] of Object.entries(routes)) {
      edges.push({ from, to: target, label: event, virtual: true, key: `virtual-${from}-${event}` });
    }
  }
  return edges;
}

function layout(ids: string[], edges: Edge[], initial: string | undefined) {
  const present = new Set(ids);
  const depth = new Map<string, number>();
  const start = initial && present.has(initial) ? initial : ids[0];
  if (start) {
    const queue = [start];
    depth.set(start, 0);
    while (queue.length) {
      const current = queue.shift() as string;
      for (const edge of edges) {
        if (edge.from !== current || !present.has(edge.to) || depth.has(edge.to)) continue;
        depth.set(edge.to, (depth.get(current) || 0) + 1);
        queue.push(edge.to);
      }
    }
  }
  let orphanColumn = Math.max(0, ...depth.values(), 0);
  for (const id of ids) {
    if (!depth.has(id)) {
      orphanColumn += 1;
      depth.set(id, orphanColumn);
    }
  }
  const columns = new Map<number, string[]>();
  for (const id of ids) {
    const column = depth.get(id) || 0;
    columns.set(column, [...(columns.get(column) || []), id]);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [column, list] of columns) {
    list.forEach((id, row) => positions.set(id, { x: 24 + column * COLUMN, y: 36 + row * (NODE_H + 28) }));
  }
  const width = Math.max(640, ...[...positions.values()].map((point) => point.x + NODE_W + 48));
  const height = Math.max(420, ...[...positions.values()].map((point) => point.y + NODE_H + 120));
  return { positions, width, height };
}

export default function WorkflowEditor({ embedded = false }: { embedded?: boolean }) {
  const [definitions, setDefinitions] = useState<Definition[]>([]);
  const [selected, setSelected] = useState("");
  const [draft, setDraft] = useState<Chart | null>(null);
  const [displayName, setDisplayName] = useState<Copy>(copyFrom(undefined));
  const [language, setLanguage] = useState<Lang>("fr");
  const [processKey, setProcessKey] = useState<string>(PROCESSES[0][0]);
  const [entityType, setEntityType] = useState("individual");
  const [stateId, setStateId] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("Parcours agence");

  useEffect(() => {
    void api<Definition[]>("/api/v1/workflow-definition")
      .then((rows) => {
        setDefinitions(rows);
        if (rows[0]) openRow(rows[0]);
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  function openRow(row: Definition) {
    setSelected(row.id);
    setDraft(structuredClone(row.definition || { states: {} }));
    setDisplayName(copyFrom(row.extensions?.i18n?.displayName, String(row.displayName || row.name || "")));
    setProcessKey(row.processKey && PROCESSES.some(([key]) => key === row.processKey) ? row.processKey : PROCESSES[0][0]);
    setEntityType(row.entityType || "individual");
    const states = row.definition?.states || {};
    setStateId(row.definition?.initial || Object.keys(states)[0] || "");
    setDirty(false);
    setMessage("");
    setError("");
  }

  function choose(row: Definition) {
    if (dirty && !window.confirm("Les modifications non enregistrées seront perdues.")) return;
    openRow(row);
  }

  const current = definitions.find((row) => row.id === selected);
  const states = draft?.states || {};
  const stored = draft ? storedEdges(draft) : [];
  const edges = !draft ? [] : stored.length ? stored : virtualEdges(processKey);
  const virtual = stored.length === 0 && edges.length > 0;
  const nodeIds = [...new Set([...Object.keys(states), ...(virtual ? edges.flatMap((edge) => [edge.from, edge.to]) : [])])];
  const graph = layout(nodeIds, edges, draft?.initial);
  const node = stateId ? states[stateId] : undefined;
  const filtered = definitions.filter((row) => queryMatches(query, {
    name: String(row.displayName || row.name),
    process: String(row.processKey || ""),
    entity: String(row.entityType || ""),
    type: String(row.definitionType || ""),
  }));
  const visible = filtered.slice(page * 12, page * 12 + 12);

  function edit(next: Chart) {
    setDraft(next);
    setDirty(true);
  }

  function updateNode(id: string, next: StateNode) {
    if (!draft) return;
    edit(patch(draft, id, next));
  }

  function renameState(nextId: string) {
    if (!draft || !stateId || nextId === stateId) return;
    const clean = nextId.trim().replace(/\s+/g, "_");
    if (!clean || states[clean]) {
      setError(states[clean] ? "Cet identifiant existe déjà." : "L'identifiant est vide.");
      return;
    }
    const renamed: Record<string, StateNode> = {};
    for (const [id, body] of Object.entries(states)) {
      renamed[id === stateId ? clean : id] = retargetNode(body, stateId, clean);
    }
    edit({ ...draft, initial: draft.initial === stateId ? clean : draft.initial, states: renamed });
    setStateId(clean);
    setError("");
  }

  function addState() {
    if (!draft) return;
    const id = `etape_${Object.keys(states).length + 1}`;
    edit({ ...draft, initial: draft.initial || id, states: { ...states, [id]: { on: {}, meta: { label: "Nouvelle étape" } } } });
    setStateId(id);
  }

  function removeState() {
    if (!draft || !stateId || !states[stateId]) return;
    const remaining: Record<string, StateNode> = {};
    for (const [id, body] of Object.entries(states)) {
      if (id === stateId) continue;
      const cleared = retargetNode(body, stateId, "");
      if (cleared.on) {
        const on: Record<string, TransitionValue> = {};
        for (const [event, value] of Object.entries(cleared.on)) {
          const kept = slotsOf({ on: { [event]: value } }).filter((slot) => slot.target);
          if (!kept.length) continue;
          on[event] = kept.length === 1 && !kept[0].cond
            ? kept[0].target
            : kept.map((slot) => ({ target: slot.target, ...(slot.cond ? { cond: slot.cond } : {}) }));
        }
        cleared.on = on;
      }
      remaining[id] = cleared;
    }
    const initial = draft.initial === stateId ? Object.keys(remaining)[0] || "" : draft.initial;
    edit({ ...draft, initial, states: remaining });
    setStateId(initial || "");
  }

  function changeSlot(slot: Slot, eventName: string, target: string) {
    if (!draft || !node) return;
    const removed = takeSlot(node.on || {}, slot);
    const value = typeof removed.value === "string" ? target : { ...(removed.value as object), target };
    const on = { ...removed.on };
    placeValue(on, eventName.trim() || slot.event, value as TransitionValue);
    updateNode(stateId, { ...node, on });
  }

  function addTransition(event: FormEvent) {
    event.preventDefault();
    if (!draft || !stateId || !states[stateId]) return;
    const target = Object.keys(states).find((id) => id !== stateId) || stateId;
    const on = { ...(states[stateId].on || {}) };
    placeValue(on, "NEXT", target);
    updateNode(stateId, { ...states[stateId], on });
  }

  function removeSlot(slot: Slot) {
    if (!node) return;
    updateNode(stateId, { ...node, on: takeSlot(node.on || {}, slot).on });
  }

  function materialize() {
    if (!draft) return;
    const flow = PROCESS_FLOW[processKey];
    if (!flow) return;
    const next: Record<string, StateNode> = { ...states };
    for (const [from, routes] of Object.entries(flow)) {
      const body = { ...(next[from] || { meta: { label: from } }) };
      const on = { ...(body.on || {}) };
      for (const [event, target] of Object.entries(routes)) {
        if (!on[event]) on[event] = target;
        if (!next[target]) next[target] = { ...(TERMINALS.has(target) ? { type: "final" } : {}), meta: { label: target } };
      }
      next[from] = { ...body, on };
    }
    edit({ ...draft, initial: draft.initial || "collect", states: next });
  }

  async function save() {
    if (!draft || !selected) return;
    setError("");
    try {
      await api(`/api/v1/workflow-definition/${selected}/definition`, { method: "PUT", body: JSON.stringify(draft) });
      const saved = await api<Definition>(`/api/v1/workflow-definition/${selected}/upgrade`, {
        method: "POST",
        body: JSON.stringify({
          displayName: preferred(displayName, current?.name || ""),
          processKey,
          entityType,
          extensions: { ...(current?.extensions || {}), i18n: { ...(current?.extensions?.i18n || {}), displayName } },
        }),
      });
      setDefinitions((rows) => rows.map((row) => (row.id === saved.id ? saved : row)));
      openRow(saved);
      setMessage("Parcours enregistré. L'exécution des dossiers suit le processus choisi.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enregistrement refusé");
    }
  }

  async function duplicate() {
    if (!selected) return;
    const copied = await api<Definition>(`/api/v1/workflow-definition/${selected}/copy`, {
      method: "POST",
      body: JSON.stringify({ name: `${current?.name || "parcours"}-copie`, displayName: `${preferred(displayName)} (copie)` }),
    });
    setDefinitions((rows) => [copied, ...rows]);
    openRow(copied);
    setMessage("Copie créée. L'original n'est pas modifié.");
  }

  async function createWorkflow(event: FormEvent) {
    event.preventDefault();
    const slug = newName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || `parcours-${Date.now()}`;
    const created = await api<Definition>("/api/v1/workflow-definition", {
      method: "POST",
      body: JSON.stringify({
        name: `${slug}-${Date.now()}`,
        displayName: newName.trim() || "Nouveau parcours",
        processKey,
        entityType,
        definition: { initial: "collect", states: { collect: { meta: { label: "Collecte" }, on: { NEXT: "review" } }, review: { meta: { label: "Revue" }, type: "final" } } },
      }),
    });
    setDefinitions((rows) => [created, ...rows]);
    openRow(created);
    setCreating(false);
    setMessage("Parcours créé. Ajoutez les étapes, puis enregistrez.");
  }

  return (
    <section className={embedded ? "" : "page"}>
      {embedded ? null : (
        <>
          <h1>Parcours</h1>
          <p className="lede">Le graphe est la définition du parcours. Chaque étape est reliée à la suivante, y compris la revue du score de risque : criblage, évaluation, revue analyste, puis contrôle checker.</p>
        </>
      )}
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="label">{message}</p> : null}
      <div className="flow-desk">
        <div className="queue">
          <QueryStack
            query={query}
            onChange={(next) => { setPage(0); setQuery(next); }}
            fields={[
              { key: "name", label: "Nom" },
              { key: "process", label: "Processus", options: PROCESSES.map(([key]) => key) },
              { key: "entity", label: "Entité", options: ["individual", "business"] },
            ]}
            compact
            total={filtered.length}
            page={page}
            pageSize={12}
            onPage={setPage}
          />
          <button className="ghost" type="button" onClick={() => setCreating((value) => !value)}>Nouveau parcours</button>
          {creating ? (
            <form className="stack" onSubmit={createWorkflow}>
              <input aria-label="Nom du parcours" value={newName} onChange={(event) => setNewName(event.target.value)} />
              <button type="submit">Créer</button>
            </form>
          ) : null}
          {visible.map((row) => (
            <button className={row.id === selected ? "flow-pick active" : "flow-pick"} key={row.id} type="button" onClick={() => choose(row)}>
              {row.displayName || row.name}
              <span>{row.processKey} · {Object.keys(row.definition?.states || {}).length} étapes</span>
            </button>
          ))}
        </div>
        <div className="flow-stage">
          <LanguageTabs language={language} onChange={setLanguage} />
          <div className="flow-toolbar">
            <label>Nom affiché · {language.toUpperCase()}
              <input dir={language === "ar" ? "rtl" : "ltr"} value={displayName[language]} onChange={(event) => { setDisplayName((current) => ({ ...current, [language]: event.target.value })); setDirty(true); }} />
            </label>
            <label>Processus exécuté
              <select value={processKey} onChange={(event) => { setProcessKey(event.target.value); setDirty(true); }}>
                {PROCESSES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </label>
            <label>Entité
              <select value={entityType} onChange={(event) => { setEntityType(event.target.value); setDirty(true); }}>
                <option value="individual">Personne physique</option>
                <option value="business">Personne morale</option>
                <option value="any">Indifférent</option>
              </select>
            </label>
            <label>Étape initiale
              <select value={draft?.initial || ""} onChange={(event) => draft && edit({ ...draft, initial: event.target.value })}>
                {Object.keys(states).map((id) => <option key={id} value={id}>{id}</option>)}
              </select>
            </label>
            <button type="button" onClick={() => void save()} disabled={!dirty}>Enregistrer</button>
            <button className="ghost" type="button" onClick={() => void duplicate()}>Dupliquer</button>
          </div>
          {current?.extensions?.originalProcessKey && current.extensions.originalProcessKey !== processKey ? (
            <p className="label">Clé d'origine conservée : {current.extensions.originalProcessKey}. Elle est exécutée par le processus sélectionné.</p>
          ) : null}
          {virtual ? (
            <p className="label">Aucune transition enregistrée. Le tracé en pointillés est celui du processus Clarity. <button className="linkish" type="button" onClick={materialize}>L'écrire dans la définition</button></p>
          ) : null}
          <div className="flow-canvas-wrap">
            <div className="flow-canvas" style={{ width: graph.width, height: graph.height }}>
              <svg width={graph.width} height={graph.height} aria-hidden="true">
                {edges.map((edge, index) => {
                  const from = graph.positions.get(edge.from);
                  const to = graph.positions.get(edge.to);
                  if (!from || !to) return null;
                  const back = to.x <= from.x;
                  const x1 = from.x + NODE_W;
                  const y1 = from.y + NODE_H / 2;
                  const x2 = to.x;
                  const y2 = to.y + NODE_H / 2;
                  const lane = 18 + (index % 4) * 14;
                  const path = back
                    ? `M ${from.x + NODE_W / 2} ${from.y + NODE_H} V ${Math.max(from.y, to.y) + NODE_H + lane} H ${to.x + NODE_W / 2} V ${to.y + NODE_H}`
                    : `M ${x1} ${y1} C ${x1 + 36} ${y1}, ${x2 - 36} ${y2}, ${x2} ${y2}`;
                  const labelX = back ? (from.x + to.x) / 2 + NODE_W / 2 : (x1 + x2) / 2;
                  const labelY = back ? Math.max(from.y, to.y) + NODE_H + lane - 4 : Math.min(y1, y2) - 8;
                  return (
                    <g key={edge.key}>
                      <path d={path} className={edge.virtual ? "flow-edge virtual" : "flow-edge"} />
                      <text x={labelX} y={labelY} className="flow-edge-label">{edge.label}</text>
                    </g>
                  );
                })}
              </svg>
              {nodeIds.map((id) => {
                const point = graph.positions.get(id);
                if (!point) return null;
                const body = states[id];
                const ghost = !body;
                const final = body?.type === "final" || ghost;
                const titled = preferred(stepCopy(body)) && preferred(stepCopy(body), id) !== id;
                return (
                  <button
                    key={id}
                    type="button"
                    className={`flow-node${id === draft?.initial ? " initial" : ""}${final ? " final" : ""}${id === stateId ? " selected" : ""}${ghost ? " ghost" : ""}`}
                    style={{ left: point.x, top: point.y, width: NODE_W }}
                    onClick={() => setStateId(id)}
                  >
                    <strong>{titled ? (stepCopy(body)[language] || preferred(stepCopy(body))) : id}</strong>
                    <span>{ghost ? "terminal" : titled ? id : id === draft?.initial ? "initiale" : final ? "finale" : "étape"}</span>
                  </button>
                );
              })}
            </div>
          </div>
          {stateId && !node ? <p className="label">{stateId} est un état terminal du processus. Il n'est pas encore dans la définition.</p> : null}
          {node ? (
            <form className="flow-inspector" onSubmit={addTransition}>
              <div className="flow-toolbar">
                <label>Identifiant<input defaultValue={stateId} key={stateId} onBlur={(event) => renameState(event.target.value)} /></label>
                <label>Libellé · {language.toUpperCase()}
                  <input
                    dir={language === "ar" ? "rtl" : "ltr"}
                    value={stepCopy(node)[language]}
                    onChange={(event) => {
                      const i18n = { ...stepCopy(node), [language]: event.target.value };
                      updateNode(stateId, { ...node, meta: { ...(node.meta || {}), label: preferred(i18n), i18n } });
                    }}
                  />
                </label>
                <label>Étiquettes
                  <input
                    value={(node.tags || []).join(", ")}
                    onChange={(event) => updateNode(stateId, { ...node, tags: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })}
                  />
                </label>
                <label className="check">Étape finale
                  <input
                    type="checkbox"
                    checked={node.type === "final"}
                    onChange={(event) => updateNode(stateId, { ...node, type: event.target.checked ? "final" : undefined })}
                  />
                </label>
                <button className="ghost" type="button" onClick={removeState}>Retirer l'étape</button>
              </div>
              <h2>Transitions</h2>
              {slotsOf(node).map((slot) => (
                <div className="flow-transition" key={`${slot.event}-${slot.index}`}>
                  <input aria-label="Événement" key={`${slot.event}-${slot.index}`} defaultValue={slot.event} onBlur={(event) => changeSlot(slot, event.target.value, slot.target)} />
                  <select aria-label="Cible" value={slot.target} onChange={(event) => changeSlot(slot, slot.event, event.target.value)}>
                    {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                  </select>
                  {slot.cond ? <span className="label">{condText(slot.cond)}</span> : null}
                  <button className="ghost" type="button" onClick={() => removeSlot(slot)}>Retirer</button>
                </div>
              ))}
              {alwaysTargets(node).length ? <p className="label">Transitions automatiques conservées : {alwaysTargets(node).join(", ")}.</p> : null}
              <button className="ghost" type="submit">Ajouter une transition</button>
              <button className="ghost" type="button" onClick={addState}>Ajouter une étape</button>
            </form>
          ) : (
            <form onSubmit={(event) => { event.preventDefault(); addState(); }}><button className="ghost" type="submit">Ajouter une étape</button></form>
          )}
        </div>
      </div>
    </section>
  );
}
