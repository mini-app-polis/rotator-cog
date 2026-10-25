"""lambda_handler, from an SQS record to Doppler, Railway and the API."""

from __future__ import annotations

from wiring import Context, World, eligible_machine_key, sqs_event

from rotator_cog import doppler, worker


def _seed(world: World) -> tuple[str, dict]:
    name = eligible_machine_key(world.inventory)
    entry = world.inventory["secrets"][name]
    world.secrets = {name: "old-key"}
    world.services = list(entry["redeploy"])
    return name, entry


def test_a_routine_rotation_hands_the_key_over_in_two_phases(world: World) -> None:
    name, entry = _seed(world)
    event = sqs_event(
        "rotation.job", {"name": name, "config": "prd", "mode": "routine"}
    )

    result = worker.lambda_handler(event, Context())

    assert result == {"batchItemFailures": []}
    new = world.secrets[name]
    assert new not in ("old-key", "")
    assert world.secrets[doppler.previous_name(name)] == "old-key"
    # Phase 1 redeploys the API alone; phase 2 every service that reads it.
    assert world.redeployed == [
        f"id-{worker.ACCEPTING_SERVICE}",
        *[f"id-{s}" for s in entry["redeploy"]],
    ]
    (recorded,) = world.recorded
    assert recorded["name"] == name and recorded["config"] == "prd"
    assert recorded["mode"] == "routine" and recorded["verification"] == "ok"
    assert recorded["retired"] is None
    assert world.notified, "the run report was delivered"


def test_a_compromised_rotation_replaces_the_key_at_once(world: World) -> None:
    name, entry = _seed(world)
    world.secrets[doppler.previous_name(name)] = "older-key"
    event = sqs_event(
        "rotation.job", {"name": name, "config": "prd", "mode": "compromised"}
    )

    assert worker.lambda_handler(event, Context()) == {"batchItemFailures": []}

    assert set(world.secrets) == {name}
    assert world.secrets[name] != "old-key"
    assert world.redeployed == [f"id-{s}" for s in entry["redeploy"]]
    (recorded,) = world.recorded
    assert recorded["verification"] == "ok" and recorded["retired"] is not None


def test_a_key_whoami_refuses_is_recorded_failed_without_its_value(
    world: World,
) -> None:
    name, _ = _seed(world)
    world.whoami_status = 401
    event = sqs_event(
        "rotation.job", {"name": name, "config": "prd", "mode": "routine"}
    )

    assert worker.lambda_handler(event, Context()) == {"batchItemFailures": []}

    (recorded,) = world.recorded
    assert recorded["verification"] == "failed"
    assert "HTTP 401" in recorded["verification_reason"]
    assert world.secrets[name] not in str(recorded)


def test_a_rotation_that_cannot_reach_doppler_is_dead_lettered(world: World) -> None:
    name, _ = _seed(world)
    world.doppler_down = True
    event = sqs_event(
        "rotation.job", {"name": name, "config": "prd", "mode": "routine"}
    )

    result = worker.lambda_handler(event, Context())

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}
    assert world.secrets == {name: "old-key"}
    assert world.recorded == []


def test_a_job_the_worker_does_not_rotate_is_acknowledged_untouched(
    world: World,
) -> None:
    _seed(world)
    event = sqs_event(
        "rotation.job",
        {"name": "NOT_IN_THE_INVENTORY", "config": "prd", "mode": "routine"},
    )

    assert worker.lambda_handler(event, Context()) == {"batchItemFailures": []}
    assert world.redeployed == [] and world.recorded == []
