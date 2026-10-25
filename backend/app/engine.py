import uuid
from pathlib import Path

import httpx

PROCESS_CATALOG = {
    "clarityKycIndividual": {
        "name": "Entrée en relation personne physique",
        "start": "collect",
        "tasks": {
            "collect": "Collecte des informations",
            "verify": "Enregistrement de la vérification",
            "screen": "Criblage",
            "score": "Évaluation du risque",
            "analystReview": "manual review",
            "checkerReview": "Approbation checker",
            "remediate": "Demande de complément",
        },
    },
    "clarityKybBusiness": {
        "name": "Entrée en relation personne morale",
        "start": "collect",
        "tasks": {
            "collect": "Collecte des informations société",
            "verify": "Contrôles documentaires",
            "screen": "Criblage des parties",
            "score": "Évaluation du risque",
            "analystReview": "manual review",
            "checkerReview": "Approbation checker",
            "remediate": "Demande de complément",
        },
    },
    "clarityAlertInvestigation": {
        "name": "Investigation d'alerte",
        "start": "investigate",
        "tasks": {
            "investigate": "Investigation",
            "checkerReview": "Validation checker",
        },
    },
    "risk-score-improvement": {
        "name": "risk-score-improvement",
        "start": "screen",
        "tasks": {
            "screen": "Criblage",
            "score": "Évaluation du score",
            "analystReview": "manual review",
            "remediate": "Demande de complément",
        },
    },
}

ADVANCE = {
    "clarityKycIndividual": {
        "collect": {"_": "verify"},
        "verify": {"_": "screen"},
        "screen": {"_": "score"},
        "score": {"_": "analystReview"},
        "analystReview": {"approve": "checkerReview", "reject": "rejected", "remediate": "remediate"},
        "checkerReview": {"approve": "approved", "reject": "rejected", "return": "analystReview"},
        "remediate": {"_": "collect"},
    },
    "clarityKybBusiness": {
        "collect": {"_": "verify"},
        "verify": {"_": "screen"},
        "screen": {"_": "score"},
        "score": {"_": "analystReview"},
        "analystReview": {"approve": "checkerReview", "reject": "rejected", "remediate": "remediate"},
        "checkerReview": {"approve": "approved", "reject": "rejected", "return": "analystReview"},
        "remediate": {"_": "collect"},
    },
    "clarityAlertInvestigation": {
        "investigate": {"close": "closed", "escalate": "checkerReview"},
        "checkerReview": {"approve": "closed", "return": "investigate"},
    },
    "risk-score-improvement": {
        "screen": {"_": "score"},
        "score": {"_": "analystReview"},
        "analystReview": {"approve": "approved", "reject": "rejected", "remediate": "remediate"},
        "remediate": {"_": "screen"},
    },
}

TERMINAL = {"approved", "rejected", "closed"}


class WorkflowEngineError(RuntimeError):
    pass


class InProcessEngine:
    """Development fallback aligned with the Flowable BPMN task graph.

    It is not Flowable. Health checks report this mode explicitly.
    """

    mode = "in-process-fallback"

    def __init__(self) -> None:
        self._instances: dict[str, dict] = {}

    def deploy(self) -> None:
        return None

    def start(self, key: str, business_key: str, variables: dict | None = None) -> tuple[str, dict]:
        if key not in PROCESS_CATALOG:
            raise WorkflowEngineError(f"unknown process {key}")
        instance_id = str(uuid.uuid4())
        task_key = PROCESS_CATALOG[key]["start"]
        self._instances[instance_id] = {
            "key": key,
            "business_key": business_key,
            "variables": dict(variables or {}),
            "current": task_key,
            "serial": 1,
        }
        return instance_id, self._task(instance_id)

    def tasks(self, instance_id: str) -> list[dict]:
        instance = self._instances.get(instance_id)
        if not instance or instance["current"] in TERMINAL:
            return []
        return [self._task(instance_id)]

    def restore(self, instance_id: str, key: str, current: str, serial: int) -> None:
        if instance_id in self._instances:
            return
        if key not in PROCESS_CATALOG or current not in PROCESS_CATALOG[key]["tasks"]:
            raise WorkflowEngineError(f"unknown process {key}")
        self._instances[instance_id] = {
            "key": key,
            "business_key": instance_id,
            "variables": {},
            "current": current,
            "serial": max(serial, 1),
        }

    def complete(self, instance_id: str, task_id: str, variables: dict) -> list[dict]:
        instance = self._instances.get(instance_id)
        if not instance:
            raise WorkflowEngineError("process instance not found")
        current = self._task(instance_id)
        if current["id"] != task_id:
            raise WorkflowEngineError("task is not active")
        instance["variables"].update(variables)
        action = variables.get("action") or "_"
        routes = ADVANCE[instance["key"]][instance["current"]]
        nxt = routes.get(action, routes.get("_"))
        if nxt is None:
            raise WorkflowEngineError(f"action {action} is not valid for {instance['current']}")
        instance["serial"] += 1
        instance["current"] = nxt
        if nxt in TERMINAL:
            return []
        return [self._task(instance_id)]

    def _task(self, instance_id: str) -> dict:
        instance = self._instances[instance_id]
        key = instance["current"]
        catalog = PROCESS_CATALOG[instance["key"]]
        return {
            "id": f"{instance_id}:{key}:{instance['serial']}",
            "taskDefinitionKey": key,
            "name": catalog["tasks"][key],
            "processInstanceId": instance_id,
        }


class FlowableEngine:
    mode = "flowable"

    def __init__(self, base_url: str, user: str, password: str, bpmn_dir: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (user, password)
        self.bpmn_dir = bpmn_dir

    def deploy(self) -> None:
        for path in sorted(self.bpmn_dir.glob("*.bpmn20.xml")):
            with path.open("rb") as handle:
                response = httpx.post(
                    f"{self.base_url}/flowable-rest/service/repository/deployments",
                    auth=self.auth,
                    files={"file": (path.name, handle, "application/xml")},
                    timeout=20,
                )
            response.raise_for_status()

    def start(self, key: str, business_key: str, variables: dict | None = None) -> tuple[str, dict]:
        response = httpx.post(
            f"{self.base_url}/flowable-rest/service/runtime/process-instances",
            auth=self.auth,
            json={
                "processDefinitionKey": key,
                "businessKey": business_key,
                "variables": _flowable_variables(variables or {}),
            },
            timeout=20,
        )
        response.raise_for_status()
        instance_id = response.json()["id"]
        tasks = self.tasks(instance_id)
        return instance_id, tasks[0]

    def tasks(self, instance_id: str) -> list[dict]:
        response = httpx.get(
            f"{self.base_url}/flowable-rest/service/runtime/tasks",
            auth=self.auth,
            params={"processInstanceId": instance_id},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", payload if isinstance(payload, list) else [])
        return [
            {
                "id": item["id"],
                "taskDefinitionKey": item.get("taskDefinitionKey"),
                "name": item.get("name"),
                "processInstanceId": instance_id,
            }
            for item in data
        ]

    def complete(self, instance_id: str, task_id: str, variables: dict) -> list[dict]:
        response = httpx.post(
            f"{self.base_url}/flowable-rest/service/runtime/tasks/{task_id}",
            auth=self.auth,
            json={"action": "complete", "variables": _flowable_variables(variables)},
            timeout=20,
        )
        response.raise_for_status()
        return self.tasks(instance_id)


def _flowable_variables(variables: dict) -> list[dict]:
    converted = []
    for name, value in variables.items():
        kind = "string"
        if isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, int):
            kind = "integer"
        converted.append({"name": name, "value": value, "type": kind})
    return converted


def build_engine(flowable_url: str, user: str, password: str, bpmn_dir: Path):
    if not flowable_url:
        return InProcessEngine(), None
    engine = FlowableEngine(flowable_url, user, password, bpmn_dir)
    try:
        engine.deploy()
    except Exception as exc:  # noqa: BLE001 — surface the outage, keep the API usable
        fallback = InProcessEngine()
        return fallback, str(exc)
    return engine, None
