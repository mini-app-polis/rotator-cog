"""An incident's expansion: the list, its order, jobs and tasks, and repeats.

Nothing here reaches a queue, the API or the board: each is a fake.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mini_app_polis.api.contract import (
    RotationIncident,
    RotationIncidentEntryStatus,
    RotationIncidentRegistration,
    RotationJobMessage,
    RotationRequest,
    TaskCreateRequest,
)
from mini_app_polis.lambda_worker import parse_message

from rotator_cog import incident, worker

INCIDENT = "7c2d4e1a-9b3f-4a6c-8d5e-1f2a3b4c5d6e"
STARTED = "2026-10-29T17:00:00+00:00"
QUEUE = "https://sqs.us-east-1.amazonaws.com/400200465748/rotator-jobs.fifo"

#: Values a careless expansion could leak. None may appear anywhere.
LEAKS = ("old-secret-value", "sk_live_abc123")

INVENTORY: dict[str, Any] = {
    "doppler_project": "proj",
    "key_prefix": {"dev": "dev_"},
    "api_base_url": "https://api.test.invalid",
    "api_base_url_dev": "https://dev-api.test.invalid",
    "cadence_days": {
        "machine-key": 90,
        "manual": 365,
        "aws-access-key": 90,
        "provider-api": 365,
        "generated": 180,
    },
    "secrets": {
        # Worker-rotated, both configs, tier 1.
        "DEEJAY_COG_API_KEY": {
            "kind": "machine-key",
            "tier": 1,
            "configs": ["prd", "dev"],
            "redeploy": ["api-kaianolevine-com"],
        },
        # A person's: manual, tier 0, prd only (configs unset).
        "GITHUB_DASHBOARD_TOKEN": {
            "kind": "manual",
            "tier": 0,
            "steps": "github.com > new token",
            "revoke": "github.com > delete the old token",
        },
        # A person's: held outside Doppler, which counts as prd.
        "RAILWAY_DB_PASSWORD": {
            "kind": "manual",
            "tier": 1,
            "configs": [],
            "steps": "railway > reset password",
        },
        # A person's: generated secrets are rotated with rotate.py.
        "OPENAI_API_KEY": {
            "kind": "generated",
            "tier": 2,
            "configs": ["prd", "dev"],
        },
        # Never rotated, no tier: last, as tier 3.
        "PUBLIC_SITE_KEY": {
            "kind": "not-rotated",
            "configs": ["prd", "dev"],
            "can": "render a widget; public by design",
        },
        # Worker-rotated, dev only, tier 0.
        "DEV_ONLY_API_KEY": {
            "kind": "machine-key",
            "tier": 0,
            "configs": ["dev"],
            "redeploy": ["api-kaianolevine-com"],
        },
    },
}

ORDER = [
    ("GITHUB_DASHBOARD_TOKEN", "prd", 0, "task"),
    ("DEEJAY_COG_API_KEY", "prd", 1, "worker"),
    ("RAILWAY_DB_PASSWORD", "prd", 1, "task"),
    ("OPENAI_API_KEY", "prd", 2, "task"),
    ("PUBLIC_SITE_KEY", "prd", 3, "task"),
    ("DEV_ONLY_API_KEY", "dev", 0, "worker"),
    ("DEEJAY_COG_API_KEY", "dev", 1, "worker"),
    ("OPENAI_API_KEY", "dev", 2, "task"),
    ("PUBLIC_SITE_KEY", "dev", 3, "task"),
]


class FakeApi:
    """The incident's record as the API keeps it, in memory."""

    def __init__(self, states: dict[tuple[str, str], str] | None = None) -> None:
        self.states = states or {}
        self.task_requested: set[tuple[str, str]] = set()
        self.registered: list[RotationIncidentRegistration] = []
        self.tasks: list[TaskCreateRequest] = []
        self.board: dict[str, TaskCreateRequest] = {}
        self.refuse_task_for: set[str] = set()

    def register(
        self, incident_id: str, registration: RotationIncidentRegistration
    ) -> RotationIncident:
        assert incident_id == INCIDENT
        self.registered.append(registration)
        entries = [
            RotationIncidentEntryStatus(
                name=e.name,
                config=e.config,
                tier=e.tier,
                handling=e.handling,
                task_requested=(e.name, e.config) in self.task_requested,
                state=self.states.get(  # type: ignore[arg-type]
                    (e.name, e.config),
                    "task-opened"
                    if (e.name, e.config) in self.task_requested
                    else "outstanding",
                ),
            )
            for e in registration.entries
        ]
        return RotationIncident(
            incident_id=incident_id,
            started_at=STARTED,  # type: ignore[arg-type]
            entries=entries,
            outstanding=[e for e in entries if e.state != "rotated"],
        )

    def mark_task_requested(self, incident_id: str, name: str, config: str) -> None:
        assert incident_id == INCIDENT
        self.task_requested.add((name, config))

    def request_task(self, request: TaskCreateRequest) -> None:
        if any(name in request.external_id for name in self.refuse_task_for):
            raise RuntimeError("task manager refused")
        self.tasks.append(request)
        # The task manager makes no second task for the same key.
        self.board.setdefault(request.external_id, request)


class Queue:
    """This cog's own queue: what the expansion sent to it."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[RotationRequest] = []
        self.fail_for = fail_for or set()

    def send(self, job: RotationRequest) -> None:
        if job.name in self.fail_for:
            raise RuntimeError("SendMessage was refused")
        self.sent.append(job)


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []
    monkeypatch.setattr(
        "mini_app_polis.pipeline_status.post_run_finding",
        lambda *a, **kw: sent.append((a, kw)),
    )
    return sent


def _expand(api: FakeApi, queue: Queue, inventory: dict[str, Any] = INVENTORY) -> None:
    worker.expand_incident(
        INCIDENT,
        run_id="m-1",
        inventory_loader=lambda: inventory,
        api_factory=lambda machine: api,
        sender_factory=lambda: queue.send,
    )


def _report(posted: list[tuple]) -> dict[str, Any]:
    ((args, kwargs),) = posted
    return {"flow": args[0], "severity": args[1], **kwargs}


# -- the list ---------------------------------------------------------------


def test_the_list_is_prd_then_dev_by_tier_with_who_rotates_each() -> None:
    entries = incident.entries_for(INVENTORY, worker.worker_rotates(INVENTORY))
    assert [(e.name, e.config, e.tier, e.handling) for e in entries] == ORDER


def test_configs_empty_or_unset_is_prd() -> None:
    assert incident.held_in({"configs": []}) == ["prd"]
    assert incident.held_in({}) == ["prd"]
    assert incident.held_in({"configs": ["dev"]}) == ["dev"]


def test_the_packaged_inventory_lists_every_entry_in_incident_order() -> None:
    inventory = worker.load_inventory()
    entries = incident.entries_for(inventory, worker.worker_rotates(inventory))
    keys = [(e.config, e.tier) for e in entries]
    assert keys == sorted(keys, key=lambda k: (incident.CONFIG_ORDER.index(k[0]), k[1]))
    assert {e.name for e in entries} == set(inventory["secrets"])
    expected = sum(len(incident.held_in(s)) for s in inventory["secrets"].values())
    assert len(entries) == expected
    # The registration the API takes accepts the whole list.
    assert len(incident.registration(entries).entries) == expected
    for e in entries:
        if e.handling == "worker":
            worker.validate(worker.sweep_mod.request_for(e.name, e.config), inventory)


# -- the expansion ----------------------------------------------------------


def test_it_registers_then_queues_compromised_jobs_and_opens_tasks_in_order(
    posted: list,
) -> None:
    api, queue = FakeApi(), Queue()
    _expand(api, queue)

    (registration,) = api.registered
    assert [
        (e.name, e.config, e.tier, e.handling) for e in registration.entries
    ] == ORDER
    assert [(j.name, j.config) for j in queue.sent] == [
        (n, c) for n, c, _, h in ORDER if h == "worker"
    ]
    assert all(
        j.mode == "compromised" and j.incident_id == INCIDENT for j in queue.sent
    )
    assert [t.external_id for t in api.tasks] == [
        f"secret-rotation.{c}.{n}.{INCIDENT}" for n, c, _, h in ORDER if h == "task"
    ]
    assert api.task_requested == {(n, c) for n, c, _, h in ORDER if h == "task"}

    report = _report(posted)
    assert report["flow"] == "rotation-incident"
    assert report["severity"] == "SUCCESS"
    assert report["incident"] == INCIDENT
    assert (report["queued"], report["tasks"], report["failed"]) == (3, 6, 0)
    text = report["text"]
    assert text.splitlines()[1].startswith(f"+ incident {INCIDENT}: ")
    assert (
        f"incident {INCIDENT}: rotation queued: DEEJAY_COG_API_KEY in prd, "
        "DEV_ONLY_API_KEY in dev, DEEJAY_COG_API_KEY in dev"
    ) in text
    assert f"incident {INCIDENT}: task opened: GITHUB_DASHBOARD_TOKEN in prd" in text


def test_repeating_the_incident_sends_and_asks_nothing_twice(posted: list) -> None:
    api, queue = FakeApi(), Queue()
    _expand(api, queue)
    # By the second delivery the first jobs have run.
    api.states = {(j.name, j.config): "rotated" for j in queue.sent}
    first_jobs, first_tasks = len(queue.sent), len(api.tasks)

    _expand(api, queue)

    assert len(queue.sent) == first_jobs
    assert len(api.tasks) == first_tasks
    assert len(api.board) == first_tasks
    assert _report(posted[1:])["skipped"] == len(ORDER)


def test_a_worker_entry_not_rotated_yet_is_queued_again_on_resume(
    posted: list,
) -> None:
    api, queue = FakeApi(), Queue()
    _expand(api, queue)
    api.states = {("DEV_ONLY_API_KEY", "dev"): "rotated"}

    _expand(api, queue)

    second = [(j.name, j.config) for j in queue.sent[3:]]
    assert second == [("DEEJAY_COG_API_KEY", "prd"), ("DEEJAY_COG_API_KEY", "dev")]


def test_a_failure_does_not_stop_the_entries_after_it(posted: list) -> None:
    api, queue = FakeApi(), Queue(fail_for={"DEEJAY_COG_API_KEY"})
    api.refuse_task_for = {"GITHUB_DASHBOARD_TOKEN"}

    _expand(api, queue)

    assert [(j.name, j.config) for j in queue.sent] == [("DEV_ONLY_API_KEY", "dev")]
    assert ("GITHUB_DASHBOARD_TOKEN", "prd") not in api.task_requested
    assert ("PUBLIC_SITE_KEY", "dev") in api.task_requested
    report = _report(posted)
    assert report["severity"] == "WARN"
    assert report["failed"] == 3
    assert "rotation not queued" in report["text"]
    assert "task not asked for" in report["text"]


def test_a_task_carries_the_command_steps_and_revoke() -> None:
    entry = INVENTORY["secrets"]["GITHUB_DASHBOARD_TOKEN"]
    task = worker.sweep_mod.incident_task_for(
        "GITHUB_DASHBOARD_TOKEN", entry, "dev", INCIDENT, why=incident.why_task(entry)
    )
    body = task.draft.body or ""
    assert task.source == "secret-rotation"
    assert task.external_id == f"secret-rotation.dev.GITHUB_DASHBOARD_TOKEN.{INCIDENT}"
    assert (
        "uv run scripts/rotate.py rotate GITHUB_DASHBOARD_TOKEN --compromised "
        f"--reason incident-{INCIDENT} --config dev"
    ) in body
    assert "Steps: github.com > new token" in body
    assert "Revoke: github.com > delete the old token" in body
    assert INCIDENT in body
    assert task.draft.labels == ["person"]


def test_a_not_rotated_task_says_it_may_not_be_invalidatable() -> None:
    entry = INVENTORY["secrets"]["PUBLIC_SITE_KEY"]
    task = worker.sweep_mod.incident_task_for(
        "PUBLIC_SITE_KEY", entry, "prd", INCIDENT, why=incident.why_task(entry)
    )
    body = task.draft.body or ""
    assert "never rotated: render a widget; public by design" in body
    assert "truly cannot be invalidated" in body
    assert "--config" not in body


def test_nothing_sent_reported_or_asked_carries_a_value(posted: list) -> None:
    inventory = json.loads(json.dumps(INVENTORY))
    api, queue = FakeApi(), Queue()
    _expand(api, queue, inventory)
    everything = json.dumps(
        [
            [j.model_dump(mode="json") for j in queue.sent],
            [t.model_dump(mode="json") for t in api.tasks],
            [r.model_dump(mode="json") for r in api.registered],
            [str(p) for p in posted],
        ]
    )
    for leak in LEAKS:
        assert leak not in everything


def test_a_registration_failure_stops_the_run_and_says_so(posted: list) -> None:
    class Down(FakeApi):
        def register(self, incident_id, registration):  # type: ignore[no-untyped-def]
            raise RuntimeError("API down")

    queue = Queue()
    with pytest.raises(RuntimeError):
        _expand(Down(), queue)
    assert queue.sent == []
    assert _report(posted)["severity"] == "WARN"


# -- the queue and the message ---------------------------------------------


class FakeSqs:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"MessageId": "m"}


def test_jobs_go_to_the_cogs_own_queue_in_its_one_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(worker.JOBS_QUEUE_URL_VAR, QUEUE)
    sqs = FakeSqs()
    send = worker.queue_sender(client=sqs)
    job = RotationRequest(
        name="DEEJAY_COG_API_KEY",
        config="prd",
        mode="compromised",
        incident_id=INCIDENT,
    )

    send(job)

    (call,) = sqs.calls
    assert call["QueueUrl"] == QUEUE
    assert call["MessageGroupId"] == "rotator"
    assert parse_message(call["MessageBody"], RotationJobMessage).payload == job


def test_without_its_queue_url_the_sender_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(worker.JOBS_QUEUE_URL_VAR, raising=False)
    with pytest.raises(RuntimeError, match=worker.JOBS_QUEUE_URL_VAR):
        worker.queue_sender(client=FakeSqs())


def test_an_incident_message_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        worker,
        "expand_incident",
        lambda incident_id, *, run_id: seen.append(incident_id),
    )
    body = json.dumps(
        {
            "type": "rotation.incident",
            "version": 1,
            "payload": {"incident_id": INCIDENT},
        }
    )
    worker.process_message(body, run_id="m-1")
    assert seen == [INCIDENT]
