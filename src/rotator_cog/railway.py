"""Railway's public GraphQL API, as the worker redeploys after a rotation.

A rotated value reaches a Railway service through Doppler's sync, and the
service reads it at boot, so after each Doppler write the worker waits for
Railway to show the new value, then redeploys and waits for the deployment.

The token is a project token, which reaches one environment of one project
and is sent as ``Project-Access-Token``; the project and environment come
from the token itself. A redeploy reuses the last successful deployment's
image: a changed variable needs no build, and a build per phase would not
fit the function's timeout.

Reading a service's variables returns every value it holds. They are hashed
against what is expected and dropped there: nothing here logs, keeps or
raises with a value, a token or a response body.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

URL = "https://backboard.railway.com/graphql/v2"
TIMEOUT_SECONDS = 30.0

#: How often a wait asks again.
POLL_SECONDS = 5.0

#: A deployment in one of these is running the new configuration.
DONE = frozenset({"SUCCESS", "SLEEPING"})
#: A deployment in one of these never will.
FAILED = frozenset({"FAILED", "CRASHED", "REMOVED", "SKIPPED"})

PROJECT_TOKEN = "query { projectToken { projectId environmentId } }"

SERVICE_INSTANCES = """
query ServiceInstances($environmentId: String!, $projectId: String!) {
  environment(id: $environmentId, projectId: $projectId) {
    name
    serviceInstances(first: 100) { edges { node { serviceId serviceName } } }
  }
}
"""

VARIABLES = """
query Variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
  variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
}
"""

LAST_SUCCESS = """
query LastSuccess($input: DeploymentListInput!) {
  deployments(input: $input, first: 1) { edges { node { id status } } }
}
"""

REDEPLOY = """
mutation Redeploy($id: String!) {
  deploymentRedeploy(id: $id, usePreviousImageTag: true) { id }
}
"""

DEPLOYMENT = "query Deployment($id: String!) { deployment(id: $id) { status } }"


class RailwayError(RuntimeError):
    """Railway refused, failed or timed out. Names and statuses, never values."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Railway:
    """The environment a project token reaches."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            headers={"Project-Access-Token": token, "Content-Type": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        scope = self._call("read the token's scope", PROJECT_TOKEN, {})["projectToken"]
        self.project_id = str(scope["projectId"])
        self.environment_id = str(scope["environmentId"])

    def _call(self, what: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                URL, json={"query": query, "variables": variables}
            )
        except httpx.HTTPError as exc:
            raise RailwayError(f"{what}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise RailwayError(f"{what}: HTTP {response.status_code}") from None
        # GraphQL failures, authorization included, come back in `errors`,
        # usually with a 200. Only their messages are kept: they name fields.
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            messages = "; ".join(str(e.get("message", "?")) for e in errors)[:300]
            raise RailwayError(f"{what}: {messages}")
        data = body.get("data") if isinstance(body, dict) else None
        if response.status_code != 200 or not isinstance(data, dict):
            raise RailwayError(f"{what}: HTTP {response.status_code}")
        return data

    def _environment(self) -> dict[str, Any]:
        data = self._call(
            "list the environment's services",
            SERVICE_INSTANCES,
            {"environmentId": self.environment_id, "projectId": self.project_id},
        )
        environment: dict[str, Any] = data["environment"]
        return environment

    def environment_name(self) -> str:
        """The name of the environment the token reaches."""
        return str(self._environment()["name"])

    def service_ids(self) -> dict[str, str]:
        """Service name to id, for every service in the environment."""
        edges = self._environment()["serviceInstances"]["edges"]
        return {e["node"]["serviceName"]: e["node"]["serviceId"] for e in edges}

    def _matches(self, service_id: str, expected: Mapping[str, str | None]) -> bool:
        held = self._call(
            "read a service's variables",
            VARIABLES,
            {
                "projectId": self.project_id,
                "environmentId": self.environment_id,
                "serviceId": service_id,
            },
        )["variables"]
        try:
            for name, want in expected.items():
                have = held.get(name)
                if (have is None) != (want is None):
                    return False
                if have is not None and digest(have) != want:
                    return False
            return True
        finally:
            del held

    def wait_for_variables(
        self, service_id: str, expected: Mapping[str, str | None], *, timeout: float
    ) -> None:
        """Wait until the service holds each value (by digest; None: absent)."""
        end = self._clock() + timeout
        while not self._matches(service_id, expected):
            if self._clock() >= end:
                names = ", ".join(sorted(expected))
                raise RailwayError(
                    f"{service_id} did not show the new {names} within {timeout:g}s"
                )
            self._sleep(POLL_SECONDS)

    def redeploy(self, service_id: str) -> str:
        """Redeploy the last successful deployment's image. The new deployment's id."""
        edges = self._call(
            "find the last successful deployment",
            LAST_SUCCESS,
            {
                "input": {
                    "projectId": self.project_id,
                    "environmentId": self.environment_id,
                    "serviceId": service_id,
                    "status": {"in": ["SUCCESS"]},
                }
            },
        )["deployments"]["edges"]
        if not edges:
            raise RailwayError(f"{service_id} has no successful deployment to redeploy")
        data = self._call("redeploy", REDEPLOY, {"id": edges[0]["node"]["id"]})
        return str(data["deploymentRedeploy"]["id"])

    def wait_for_deployments(
        self, deployment_ids: list[str], *, timeout: float
    ) -> None:
        """Wait until every deployment is running. Raises when one fails or time runs out."""
        end = self._clock() + timeout
        pending = list(deployment_ids)
        while True:
            still: list[str] = []
            for deployment_id in pending:
                status = self._call(
                    "read a deployment", DEPLOYMENT, {"id": deployment_id}
                )["deployment"]["status"]
                if status in FAILED:
                    raise RailwayError(f"deployment {deployment_id} ended {status}")
                if status not in DONE:
                    still.append(deployment_id)
            if not still:
                return
            if self._clock() >= end:
                raise RailwayError(
                    f"{len(still)} deployment(s) not running within {timeout:g}s"
                )
            pending = still
            self._sleep(POLL_SECONDS)
