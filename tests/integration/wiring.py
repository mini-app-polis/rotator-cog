"""Fakes for the worker's far ends, wired to each other as the real ones are.

Everything the handler builds is the real thing — the inventory packaged
with the function, the Doppler and Railway clients, the whoami check, the
API client that records the rotation and delivers the run report. Only the
far ends are fakes: HTTP through respx, and Parameter Store through a boto3
stand-in, since boto3 ships in the Lambda runtime and not here.

The fakes are wired to each other the way the real systems are: Railway
shows a service whatever Doppler holds (Doppler's sync), through references
to every inventory name and its previous slot, as production's
.railway/railway.ts declares them, so a reference Doppler does not fill
shows empty; and whoami names a machine only for the key Doppler holds for
it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from rotator_cog import worker

API = "https://api.test.invalid"
DOPPLER_TOKEN = "dp.st.prd.token-for-integration-tests"
RAILWAY_TOKEN = "railway-project-token-for-integration-tests"
PARAMETERS = {
    "/rotator/doppler/prd": DOPPLER_TOKEN,
    "/rotator/railway/prd": RAILWAY_TOKEN,
}


def eligible_machine_key(inventory: dict[str, Any]) -> str:
    """A machine key the worker rotates in prd, with callers besides the API."""
    for name in sorted(inventory["secrets"]):
        try:
            entry = worker.validate(
                worker.sweep_mod.request_for(name, "prd"), inventory
            )
        except worker.InvalidJob:
            continue
        if entry["kind"] == "machine-key" and len(entry["redeploy"]) > 1:
            return name
    raise AssertionError("the inventory has no machine key with callers")


class World:
    """Doppler's prd config, Railway's production environment and the API."""

    def __init__(self, inventory: dict[str, Any]) -> None:
        self.inventory = inventory
        self.secrets: dict[str, str] = {}
        self.services: list[str] = []
        self.redeployed: list[str] = []
        self.recorded: list[dict[str, Any]] = []
        self.notified: list[dict[str, Any]] = []
        self.whoami_status: int | None = None
        self.doppler_down = False

    # -- Doppler ---------------------------------------------------------

    def doppler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {DOPPLER_TOKEN}"
        if self.doppler_down:
            raise httpx.ConnectError("Doppler is down")
        if request.method == "GET" and request.url.path.endswith("/names"):
            return httpx.Response(200, json={"names": sorted(self.secrets)})
        if request.method == "GET":
            name = request.url.params["name"]
            if name not in self.secrets:
                return httpx.Response(404, json={"messages": ["not found"]})
            return httpx.Response(200, json={"value": {"raw": self.secrets[name]}})
        for change in json.loads(request.content)["change_requests"]:
            if change.get("shouldDelete"):
                del self.secrets[change["name"]]
            else:
                self.secrets[change["name"]] = change["value"]
        return httpx.Response(200, json={"secrets": {}})

    # -- Railway ---------------------------------------------------------

    def railway(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Project-Access-Token"] == RAILWAY_TOKEN
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        data: dict[str, Any]
        if "projectToken" in query:
            data = {"projectToken": {"projectId": "p", "environmentId": "e"}}
        elif "serviceInstances" in query:
            edges = [
                {"node": {"serviceId": f"id-{s}", "serviceName": s}}
                for s in self.services
            ]
            data = {
                "environment": {
                    "name": "production",
                    "serviceInstances": {"edges": edges},
                }
            }
        elif "variables(" in query:
            data = {"variables": self.referenced()}
        elif "deploymentRedeploy" in query:
            self.redeployed.append(variables["id"].removeprefix("last-"))
            data = {"deploymentRedeploy": {"id": f"new-{variables['id']}"}}
        elif "deployments(" in query:
            service = variables["input"]["serviceId"]
            data = {
                "deployments": {
                    "edges": [{"node": {"id": f"last-{service}", "status": "SUCCESS"}}]
                }
            }
        else:
            data = {"deployment": {"status": "SUCCESS"}}
        return httpx.Response(200, json={"data": data})

    def referenced(self) -> dict[str, str]:
        """What a service shows: each reference, rendered from Doppler or empty."""
        names = set(self.secrets)
        for name, entry in self.inventory["secrets"].items():
            names |= {name, worker.doppler_mod.previous_name(name)}
            if entry.get("id_var"):
                names.add(entry["id_var"])
        return {n: self.secrets.get(n, "") for n in names}

    # -- the API ---------------------------------------------------------

    def whoami(self, request: httpx.Request) -> httpx.Response:
        if self.whoami_status is not None:
            return httpx.Response(self.whoami_status, json={})
        presented = request.headers["Authorization"].removeprefix("Bearer ")
        for name, value in self.secrets.items():
            if value == presented and name.endswith("_API_KEY"):
                subject = worker.machine_name(name)
                return httpx.Response(200, json={"verified": {"subject": subject}})
        return httpx.Response(401, json={})

    def record(self, request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)
        self.recorded.append(event)
        stored = {
            **event,
            "id": str(uuid.uuid4()),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        return _envelope(stored)

    def notify(self, request: httpx.Request) -> httpx.Response:
        self.notified.append(json.loads(request.content))
        return _envelope({"forwarded": True, "event": "notify"})


def _envelope(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"data": data, "meta": {"count": 1, "total": 1, "version": "test"}}
    )


class _Ssm:
    """Parameter Store, holding the rotator's two prd tokens."""

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803 — boto3's names
        assert WithDecryption
        return {"Parameter": {"Value": PARAMETERS[Name] + "\n"}}


def ssm_client() -> _Ssm:
    """What boto3.client("ssm") returns here."""
    return _Ssm()


class Context:
    """The part of Lambda's context the handler reads."""

    def get_remaining_time_in_millis(self) -> int:
        return 900_000


def sqs_event(message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One SQS record carrying one message, as the queue delivers it."""
    body = json.dumps({"type": message_type, "version": 1, "payload": payload})
    return {
        "Records": [
            {
                "messageId": "msg-1",
                "body": body,
                "attributes": {"ApproximateReceiveCount": "1"},
            }
        ]
    }
