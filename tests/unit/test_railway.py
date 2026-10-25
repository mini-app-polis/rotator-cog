"""Railway's API against a fake transport: never the real Railway."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from rotator_cog import railway

TOKEN = "railway-project-token-for-tests"
VALUE = "dev_value-for-tests"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeApi:
    """Answers each query by its operation; records every request."""

    def __init__(self) -> None:
        self.variables: list[dict[str, str]] = [{"KEY": VALUE}]
        self.statuses: dict[str, list[str]] = {}
        self.successful: list[str] = ["dep-1"]
        self.errors: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"headers": request.headers, **body})
        query, variables = body["query"], body["variables"]
        for op, message in self.errors.items():
            if op in query:
                return httpx.Response(200, json={"errors": [{"message": message}]})
        if "projectToken" in query:
            data: Any = {"projectToken": {"projectId": "p", "environmentId": "e"}}
        elif "serviceInstances" in query:
            data = {
                "environment": {
                    "name": "development",
                    "serviceInstances": {
                        "edges": [{"node": {"serviceId": "s-1", "serviceName": "api"}}]
                    },
                }
            }
        elif "variables(" in query:
            held = (
                self.variables.pop(0) if len(self.variables) > 1 else self.variables[0]
            )
            data = {"variables": held}
        elif "deployments(" in query:
            edges = [{"node": {"id": i, "status": "SUCCESS"}} for i in self.successful]
            data = {"deployments": {"edges": edges}}
        elif "deploymentRedeploy" in query:
            data = {"deploymentRedeploy": {"id": f"new-{variables['id']}"}}
        else:
            statuses = self.statuses[variables["id"]]
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            data = {"deployment": {"status": status}}
        return httpx.Response(200, json={"data": data})

    def client(self, clock: Clock | None = None) -> railway.Railway:
        clock = clock or Clock()
        return railway.Railway(
            TOKEN,
            transport=httpx.MockTransport(self.handle),
            sleep=clock.sleep,
            clock=clock,
        )


def test_the_token_names_its_project_and_environment() -> None:
    api = FakeApi()
    client = api.client()
    assert (client.project_id, client.environment_id) == ("p", "e")
    assert api.requests[0]["headers"]["Project-Access-Token"] == TOKEN
    assert client.environment_name() == "development"
    assert client.service_ids() == {"api": "s-1"}


def test_variables_are_awaited_until_they_match_by_digest() -> None:
    api = FakeApi()
    api.variables = [{"KEY": "old"}, {"KEY": "old"}, {"KEY": VALUE, "OTHER": "x"}]
    clock = Clock()
    api.client(clock).wait_for_variables(
        "s-1", {"KEY": railway.digest(VALUE), "GONE": None}, timeout=60
    )
    assert clock.now == 2 * railway.POLL_SECONDS


def test_variables_that_never_match_time_out_naming_no_value() -> None:
    api = FakeApi()
    api.variables = [{"KEY": "old"}]
    with pytest.raises(
        railway.RailwayError, match="did not show the new KEY"
    ) as caught:
        api.client().wait_for_variables(
            "s-1", {"KEY": railway.digest(VALUE)}, timeout=12
        )
    assert "old" not in str(caught.value) and VALUE not in str(caught.value)


def test_a_variable_that_should_be_gone_must_be_gone() -> None:
    api = FakeApi()
    api.variables = [{"KEY": VALUE}]
    with pytest.raises(railway.RailwayError):
        api.client().wait_for_variables("s-1", {"KEY": None}, timeout=6)


def test_variable_names_list_references_even_when_empty() -> None:
    api = FakeApi()
    api.variables = [{"KEY": VALUE, "KEY_PREVIOUS": ""}]
    assert api.client().variable_names("s-1") == {"KEY", "KEY_PREVIOUS"}


def test_a_redeploy_reuses_the_last_successful_deployments_image() -> None:
    api = FakeApi()
    assert api.client().redeploy("s-1") == "new-dep-1"
    listed, redeployed = api.requests[-2:]
    assert listed["variables"]["input"]["status"] == {"in": ["SUCCESS"]}
    assert "usePreviousImageTag: true" in redeployed["query"]


def test_a_service_with_nothing_to_redeploy_is_an_error() -> None:
    api = FakeApi()
    api.successful = []
    with pytest.raises(railway.RailwayError, match="no successful deployment"):
        api.client().redeploy("s-1")


def test_deployments_are_awaited_until_all_are_running() -> None:
    api = FakeApi()
    api.statuses = {"a": ["BUILDING", "DEPLOYING", "SUCCESS"], "b": ["SUCCESS"]}
    clock = Clock()
    api.client(clock).wait_for_deployments(["a", "b"], timeout=60)
    assert clock.now == 2 * railway.POLL_SECONDS


@pytest.mark.parametrize("status", ["FAILED", "CRASHED", "REMOVED"])
def test_a_deployment_that_fails_fails_the_wait(status: str) -> None:
    api = FakeApi()
    api.statuses = {"a": ["DEPLOYING", status]}
    with pytest.raises(railway.RailwayError, match=f"ended {status}"):
        api.client().wait_for_deployments(["a"], timeout=60)


def test_a_deployment_that_never_finishes_times_out() -> None:
    api = FakeApi()
    api.statuses = {"a": ["DEPLOYING"]}
    with pytest.raises(railway.RailwayError, match="not running within 12s"):
        api.client().wait_for_deployments(["a"], timeout=12)


def test_graphql_errors_keep_only_their_messages() -> None:
    api = FakeApi()
    api.errors = {"projectToken": "Not Authorized"}
    with pytest.raises(railway.RailwayError, match="Not Authorized") as caught:
        api.client()
    assert TOKEN not in str(caught.value)


def test_an_unreachable_api_names_only_the_error_type() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route for {TOKEN}")

    with pytest.raises(railway.RailwayError, match="ConnectError") as caught:
        railway.Railway(TOKEN, transport=httpx.MockTransport(fail))
    assert TOKEN not in str(caught.value)


def test_a_non_json_answer_is_an_error() -> None:
    with pytest.raises(railway.RailwayError, match="HTTP 502"):
        railway.Railway(
            TOKEN,
            transport=httpx.MockTransport(lambda r: httpx.Response(502, text="x")),
        )


def test_a_referenced_variable_that_renders_empty_counts_as_gone() -> None:
    api = FakeApi()
    api.variables = [{"KEY": VALUE, "KEY_PREVIOUS": ""}]
    api.client().wait_for_variables(
        "s-1", {"KEY": railway.digest(VALUE), "KEY_PREVIOUS": None}, timeout=6
    )


# -- retries ---------------------------------------------------------------


def _flaky(failures: list[Any], api: FakeApi) -> httpx.MockTransport:
    """Fail the first request whose query holds each key, then answer as ``api``."""

    def handle(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        for op in list(failures):
            if op[0] in query:
                failures.remove(op)
                if isinstance(op[1], Exception):
                    raise op[1]
                return httpx.Response(op[1], text="busy")
        return api.handle(request)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), 502, 429]
)
def test_a_query_that_blips_is_asked_again(failure: Any) -> None:
    api = FakeApi()
    client = railway.Railway(
        TOKEN, transport=_flaky([("projectToken", failure)], api), sleep=Clock().sleep
    )
    assert client.project_id == "p"


def test_a_query_that_keeps_failing_fails_after_every_attempt() -> None:
    calls: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(railway.RailwayError, match="ReadTimeout"):
        railway.Railway(TOKEN, transport=httpx.MockTransport(fail))
    assert len(calls) == railway.ATTEMPTS


def test_a_redeploy_that_was_never_sent_is_sent_again() -> None:
    api = FakeApi()
    client = railway.Railway(
        TOKEN,
        transport=_flaky([("deploymentRedeploy", httpx.ConnectError("down"))], api),
    )
    assert client.redeploy("s-1") == "new-dep-1"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), 502])
def test_a_redeploy_that_may_have_started_is_not_sent_again(failure: Any) -> None:
    api = FakeApi()
    client = railway.Railway(
        TOKEN, transport=_flaky([("deploymentRedeploy", failure)], api)
    )
    with pytest.raises(railway.RailwayError):
        client.redeploy("s-1")
    assert not any("deploymentRedeploy" in r["query"] for r in api.requests)
