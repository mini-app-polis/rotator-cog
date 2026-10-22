# Secrets

Doppler is where every secret is edited. `mini-app-polis-ecosystem` /
`prd` syncs to Railway's shared variables (production) and to Parameter
Store (the Lambda workers); `dev` syncs to Railway's development
environment. GitHub Actions secrets hold a few values of their own.

- `inventory.yaml`: every credential by name. It records what each one
  can do, where it is held, how to rotate it and how to check the new
  value. It never holds a value.
- `rotations.jsonl`: the record of every rotation, one line each,
  appended by `scripts/rotate.py` and committed. Git history is the audit
  trail.
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
5. **Record.** One line per secret goes into `rotations.jsonl`.

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
