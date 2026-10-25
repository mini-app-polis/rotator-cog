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
- `rotations.jsonl`: the record from before the API kept one. Nothing
  writes to it. `rotate.py import-ledger` seeds the API from it, and it
  stays until that import has run against production.
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

Reading or writing the record (`list`, `rotate`, `verify`, `pg-roles`,
`import-ledger`) asks for a Clerk session token at a hidden prompt: the API
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
   `ok` only if its check passed; one with no automatic check is recorded
   `failed`, saying no check ran. `verify` re-runs the checks later and
   rewrites the latest event's result.

There are two modes:

- **Routine** (the default) keeps the old credential until the new one
  verifies.
- **`--compromised`** kills the old credential as soon as the new one is
  stored, and accepts the outage.

### Known limit

The API holds one key per machine and reads it at boot. A machine-key
rotation therefore always has a short 401 window: it opens when Doppler
syncs and closes when `api-kaianolevine-com` redeploys. The Lambda cog
picks up its new key on its next invocation. Zero-downtime rotation needs
the API to accept `<NAME>_API_KEY_PREVIOUS` during an overlap. That is
not built yet.

### Importing the legacy record

`uv run scripts/rotate.py import-ledger --dry-run` prints what the import
would send. Without `--dry-run` it sends it, then reads the latest event
per secret and config back and checks each imported one is there. It sends
only the latest rotation per secret and config, prd and dev: a re-check row
decides that rotation's result, and rows with no Doppler config, rows for
database URL variables (`postgres-role`) and names no longer in the
inventory are left out, each counted. Events are keyed on secret, config
and when the value was minted, so running it again adds nothing.

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
