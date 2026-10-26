# Secrets

Doppler is where every secret is edited. `mini-app-polis-ecosystem` /
`prd` syncs to Railway's shared variables (production) and to Parameter
Store (the Lambda workers); `dev` syncs to Railway's development
environment. GitHub Actions secrets hold a few values of their own.

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
`miniapppolis`), `gh` (logged in with `admin:org` for org secrets), `curl`.

Reading or writing the record (`list`, `rotate`, `verify`, `pg-roles`) asks
for a Clerk session token at a hidden prompt: the API
records the rotation as you, and a session is how a person signs in to it.
On a page signed in to the wcs Clerk tenant, run
`await Clerk.session.getToken()` in the browser console and paste the
result. A token lasts about a minute, so the script asks again when the API
says it has expired.

Each run:

1. **Mint and store.** Machine keys and generated values are minted here
   and piped to Doppler and to any GitHub secrets that also hold them. AWS
   keys are minted with `aws iam create-access-key`. A console-issued value
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
live in Doppler alone (the rest stay on `rotate.py`). An access key is
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
  is deleted from Doppler and the API redeployed. Because the sweep runs
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
- **Reports drift**, as `rotate.py check` does, in the run report.

In development, a service reads only the shared variables referenced on
it in the dashboard, so `api-kaianolevine-com` there needs each
`<NAME>_API_KEY_PREVIOUS` referenced (`${{shared.NAME_API_KEY_PREVIOUS}}`)
before the worker can rotate that key in dev. Production's references are
in the service's `.railway/railway.ts`. The worker checks before it writes
anything: a rotation whose services are missing, or do not reference a
variable it would wait for, is reported as "not ready to rotate; nothing
was written", naming the reference to add. A reference to a shared
variable Doppler does not hold yet is fine; it renders empty until the
first rotation fills it.

## Adding a secret

Add the secret to `inventory.yaml` in the same change that adds it to
Doppler. `rotate.py check` exits non-zero when a secret-looking name in
Doppler is missing from the inventory.

Doppler puts a new name into Railway's shared variables, but a service only
reads the names it references. In production the service's
`.railway/railway.ts` declares the reference. Development is configured in
the dashboard, so add the reference there by hand: on the development
service, set the variable to `${{shared.NAME}}`. Otherwise development
starts without the value, and a machine key logs
`[identity] <machine> has no <NAME>; it cannot authenticate` at startup.

## Compromise runbook

Use this when a place that holds our secrets is breached. The first use
was the Railway incident of 2026-10-21, in which environment variables
were read decrypted. Everything in Doppler was in Railway, so everything
in Doppler is treated as exposed.

Work tier by tier. Rotate each tier with
`rotate --railway-exposed --tier N --compromised --reason "..."`, once for
`--config prd` and once for `--config dev`.

0. **Secret store and source code first.** Rotate
   `DOPPLER_SPOTIFY_WRITE_TOKEN` before anything else. It can read the
   Doppler config, so whoever holds it would also read every value rotated
   after it. Revoke each GitHub token before re-minting it, then check that
   it is still needed at all. Re-mint the Railway tokens: Railway
   invalidated them, so the IaC applies and db-drill stay broken until
   this is done.
1. **Identity, data, infrastructure.** This covers the Clerk secret key,
   both Postgres passwords (use Railway's Regenerate, never a hand edit),
   the four AWS keys, the Google service-account keys, Brevo, and every
   machine key.
2. **Money and third parties.** This covers the model API keys, Asana,
   Spotify, Turnstile and the webhook secrets. Rotate the Spotify client
   secret before the refresh token, then use the re-auth link.
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
