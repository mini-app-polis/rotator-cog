# Secrets

Doppler is where every secret is edited, once, in
`mini-app-polis-ecosystem` (`prd` and `dev`). Everything that reads one
holds a reference to it in a Doppler project of its own: each Railway
service, each Lambda worker (through Parameter Store) and each
repository's GitHub Actions secrets and variables. A value written to the
ecosystem project reaches all of them through Doppler's syncs, so a
rotation writes Doppler and nothing else.

- `inventory.yaml`: every credential by name. It records what each one
  can do, where it is held, how to rotate it and how to check the new
  value. It never holds a value.
- The record of every rotation is kept by the API, not here: one event
  per rotation of a secret in a Doppler config, at
  `/v1/secrets/rotation-events`. `scripts/rotate.py` writes it under your
  own identity and reads it back to say what is due.
- `../scripts/rotate.py`: does the rotation.

## Using it

```sh
uv run scripts/rotate.py list --due                 # what is overdue
uv run scripts/rotate.py check                      # Doppler prd vs the inventory
uv run scripts/rotate.py rotate OPENAI_API_KEY --reason "routine"
uv run scripts/rotate.py rotate --kind machine-key --reason "routine" --dry-run
```

Prerequisites on the Mac: `doppler` (logged in), `aws` (profile
`miniapppolis`), `curl`.

Reading or writing the record (`list`, `rotate`, `verify`, `pg-roles`) asks
for a Clerk session token at a hidden prompt: the API
records the rotation as you, and a session is how a person signs in to it.
On a page signed in to the wcs Clerk tenant, run
`await Clerk.session.getToken()` in the browser console and paste the
result. A token lasts about a minute, so the script asks again when the API
says it has expired.

Each run:

1. **Mint and store.** Machine keys and generated values are minted here
   and piped to Doppler. AWS keys are minted with
   `aws iam create-access-key`. A console-issued value
   is pasted at a hidden prompt.
2. **Redeploy.** The script lists the Railway services that read the
   secret at boot and waits until they have been redeployed. Lambda
   workers re-read Parameter Store on every invocation and need nothing.
3. **Verify.** The inventory's `verify` command runs under `doppler run`,
   so the value reaches it as an environment variable. Machine keys are
   checked against `GET /v1/identity/whoami`.
4. **Revoke the old value.** Old AWS keys are deleted automatically. For
   console-issued values the script prints the step and asks for
   confirmation.
5. **Record.** One event per secret goes to the API. A rotation is
   `ok` if its check passed and `failed` if it did not. One with no
   automatic check is recorded `unchecked`: it counts as rotated, and a
   broken value shows up in the failures of whatever uses it. `verify`
   re-runs the checks later and rewrites the latest event's result.

There are two modes:

- **Routine** (the default) keeps the old credential until the new one
  verifies.
- **`--compromised`** kills the old credential as soon as the new one is
  stored, and accepts the outage.

### Rotation by the worker

The rotator's Lambda worker rotates machine keys and IAM access keys that
need no manual step (the rest stay on `rotate.py`). An access key is
created beside the old one, written to Doppler with its id, checked with
`sts get-caller-identity` once the services have redeployed, and the old
key deleted only then; a user that already has two keys is refused for a
person to sort out. A machine key is rotated with no 401 window: the API accepts a
machine's `<NAME>_API_KEY_PREVIOUS` beside its current key. A routine
rotation puts the new key in the previous slot and redeploys the API, then
makes it the current key with the old one in the previous slot and
redeploys again, so callers switch while both are accepted. Ask for one
with `POST /v1/secrets/rotations` on the production API (only production
has the rotator's queue), with `config` naming the Doppler config.

A daily sweep per config (14:00 UTC) does the rest:

- **Retires previous keys.** Once nothing has used a previous key for 24
  hours (the API's audit trail records which key proved each request), it
  is emptied in Doppler (never deleted: services' configs reference the
  slot; ADR-0005) and the API redeployed. Because the sweep runs
  once a day, a previous key is retired 24 to 48 hours after its last use.
  In dev the rotator cannot read the development API's audit trail, so a
  previous key is retired a day after its rotation. At seven days a key
  still in use is retired anyway, and the run report names the machine
  still on it.
- **Rotates the most overdue key**, one per config per day, on a day that
  retired nothing, once `ROTATOR_SWEEP_ROTATE` is `true` in Doppler prd.
  Until then it only reports what is due. A key mid-handover is not rotated again, and one whose
  last rotation failed is reported each day rather than retried. A key the
  services are not ready for is reported "not ready to rotate" and passed
  over for the next most overdue, so it cannot hold up the rest.
- **Asks for a task per due manual secret**, once `ROTATOR_SWEEP_TASKS`
  is `true` in Doppler prd. Until then it only reports which are due. A
  manual secret is due when it has never been recorded as rotated, its
  cadence has passed, or its last rotation was recorded as failed. The task
  (`ROT-`, tagged `person`) gives the `rotate.py rotate` command, the steps
  and the revoke, never a value. The sweep asks under a key naming the
  rotation the secret follows, so it gets one task until it is rotated. A
  secret held outside Doppler is asked for by the prd sweep.
- **Reports drift**, as `rotate.py check` does, in the run report.
- **Flags changes no rotation explains.** Doppler's secrets.update webhook
  tells the API which names changed (never values). Once a change is a day
  old, the sweep checks it against the record: a change to a secret is
  explained by a rotation of it minted from two hours before to five
  minutes after, and a change to its `_PREVIOUS` slot, key id or copies by
  its rotation in the eight days before. Anything else was changed outside
  this process: it is in the run report, and, with `ROTATOR_SWEEP_TASKS`
  on, one task asks you to record it with `rotate.py` or revert it.

A Railway service holds only the names its own Doppler config references,
so `api-kaianolevine-com` needs each `<NAME>_API_KEY_PREVIOUS` referenced
in its config for that environment before the worker can rotate the key
there. The worker checks before it writes anything: a rotation whose
services are missing, or do not hold a variable it would wait for, is
reported as "not ready to rotate; nothing was written", naming what to
reference. A previous slot is always present in the ecosystem project,
empty when no handover is under way (ADR-0005), so the reference can be
added at any time.

## Adding a secret

Add the secret to `inventory.yaml` in the same change that adds it to
Doppler's `mini-app-polis-ecosystem`. `rotate.py check` exits non-zero
when a secret-looking name there is missing from the inventory.

Then reference it from whatever reads it: a name in the reader's own
Doppler config, `${mini-app-polis-ecosystem.<config>.NAME}`. The
ecosystem's Terraform for Doppler (in mini-app-polis/infra) declares which
names each reader takes. A Railway service also declares the name in its
`.railway/railway.ts`, or the next apply removes it. Until it is
referenced, the reader starts without the value, and a machine key logs
`[identity] <machine> has no <NAME>; it cannot authenticate` at startup.

## Compromise runbook

Use this when a place that holds our secrets is breached. The first use
was the Railway incident of 2026-10-21, in which environment variables
were read decrypted. Everything in Doppler was in Railway, so everything
in Doppler is treated as exposed.

Work tier by tier. Rotate each tier with
`rotate --railway-exposed --tier N --compromised --reason "..."`, once for
`--config prd` and once for `--config dev`.

0. **Secret store and source code first.** Rotate the Doppler tokens
   before anything else: whoever holds one reads every value in its config,
   including the ones rotated after it. The rotator's own two tokens reach
   the vault, so they come first. Revoke each GitHub token before re-minting it, then check that
   it is still needed at all. Re-mint the Railway tokens: Railway
   invalidated them, so the IaC applies and db-drill stay broken until
   this is done.
1. **Identity, data, infrastructure.** This covers the Clerk secret key,
   both Postgres passwords (use Railway's Regenerate, never a hand edit),
   the four AWS keys, the Google service-account keys, Brevo, and every
   machine key.
2. **Money and third parties.** This covers the model API keys, Asana,
   Spotify, Turnstile and the webhook secrets. Rotate the Spotify client
   secret and `DOPPLER_SPOTIFY_WRITE_TOKEN` (it can read the refresh token)
   before the refresh token, then use the re-auth link.
3. **Nuisance.** This covers the Discord webhook URLs and the Healthchecks
   ping URLs.

Then check for misuse, back to the start of the month before the
disclosure:

- **AWS:** CloudTrail events for the IAM users `evaluator-producer`,
  `db-backup-writer`, `db-backup-reader` and `db-poller`. For the reader,
  look for `GetObject` on the backup bucket in particular.
- **GitHub:** the audit log of both orgs (pushes, new deploy keys, new
  PATs, workflow changes), and the security log of each token's owner.
- **Clerk:** new sessions and API-created users in the wcs tenant.
- **Postgres:** unknown roles (`\du`) and connections from addresses that
  are not Railway's.
- **Model and email providers:** OpenAI, Anthropic and Brevo usage for
  spend and sends you did not make.
- **Doppler:** the activity log for reads by service tokens.
