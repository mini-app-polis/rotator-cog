# Which manual secrets the rotator could rotate

Every `kind: manual` entry in `secrets/inventory.yaml` is minted by a person
in a provider's console. The rotator could take one over only by holding an
admin credential at that provider, so the question per provider is whether
that credential is less dangerous than the secret it would rotate.

**The test.** Compare the permission the rotator would need with what the
secret itself can do (the inventory's `can` and `tier`). If the rotator's
permission is as powerful as the secret, or more, automating adds risk: a
leak of the rotator would then be worse than a leak of the secret. Such a
provider stays manual-kind and is rotated with `scripts/rotate.py`.

This document records the verdicts. It changes no entry's kind: only a
follow-up change that automates a provider does that, and the owner may
automate a provider that fails the test, recorded with the reason below.

## Verdicts

| Provider | Entries | Permission the rotator would need | What the secret can do | Verdict |
|---|---|---|---|---|
| Cloudflare API tokens | `CLOUDFLARE_API_TOKEN` (tier 0) | API Tokens: Edit, to roll a token (`PUT /user/tokens/{id}/value`) | change the account within the token's permissions (DNS, Pages, WAF) | **Manual.** A credential that can roll tokens can mint a token with any of the account's permissions, more than the infra token holds. |
| Cloudflare Turnstile | `TURNSTILE_SECRET_KEY` (tier 2) | Turnstile Sites: Edit, for `rotate_secret` on the widget | pass Turnstile checks on the contact form | **Automated (`kind: provider-api`), by the owner's decision of 2026-10-26.** It fails the test: Edit on Turnstile can also change or delete the widget, which is more than passing its checks. It was automated anyway because the rotator's priority is that rotation and invalidation are a defined, tracked process, a breach is answered by invalidating and rotating everything, and the blast radius stays inside Turnstile. The rotator's token, `ROTATOR_CLOUDFLARE_TURNSTILE_TOKEN`, holds that one permission and is itself a manual secret. |
| Doppler service tokens | `DOPPLER_SPOTIFY_WRITE_TOKEN`, `ROTATOR_DOPPLER_TOKEN_PRD`, `ROTATOR_DOPPLER_TOKEN_DEV` (tier 0) | a workplace or project role that creates tokens, above a single config's read/write | read and write one Doppler config | **Manual.** Creating tokens needs more than any one config. The rotator's own tokens are break-glass by design and are never rotated by the rotator. |
| GitHub fine-grained PATs | `GITHUB_DASHBOARD_TOKEN`, `GITHUB_ACTIONS_TOKEN` (tier 0) | none: GitHub has no API that creates a fine-grained personal access token | per the token's scope | **Manual.** Only a person can mint one. |
| GitHub App private key | `PROMOTE_APP_PRIVATE_KEY` (tier 0) | none: an App's private keys are generated in its settings in the web UI | act as the promote App on every repository it is installed on | **Manual.** Its first rotation goes through the sweep's manual-secret tasks and `rotate.py confirm` (a later change). |
| Railway tokens | `RAILWAY_TOKEN`, `DB_DRILL_RAILWAY_TOKEN`, `ROTATOR_RAILWAY_TOKEN_PRD`, `ROTATOR_RAILWAY_TOKEN_DEV` (tier 0) | an account or workspace token, to create project tokens | drive one environment of the project | **Manual.** The credential that mints project tokens reaches every environment. |
| Railway Postgres | `POSTGRES_KAIANOLEVINE_PASSWORD`, `POSTGRES_DEEJAYTOOLS_PASSWORD` (tier 1) | the superuser and a public TCP proxy on the database | read and write the whole database | **Manual.** The rotator would hold the database outright. `rotate.py pg-roles` stays the path. |
| Clerk | `CONTRACT_CLERK_SECRET_KEY` (tier 1) | none known: secret keys are added in the dashboard | full backend access to the deejaytools development instance | **Manual.** No API mints a secret key; any credential that could would hold the instance itself. |
| Google Cloud | `GOOGLE_CREDENTIALS_JSON` (tier 1) | `iam.serviceAccountKeys.create` on the fleet's service account | act as that service account on everything shared with it | **Manual.** Minting keys for the account is the same power as being it. |
| Brevo | `BREVO_API_KEY` (tier 1) | none: the API creates keys only for corporate sub-accounts | send email as the domains | **Manual.** |
| OpenAI | `OPENAI_API_KEY` (tier 2) | an organization admin key, to create a project service account and its key | spend on the account | **Manual.** An admin key manages the whole organization. |
| Anthropic | `ANTHROPIC_API_KEY` (tier 2) | none: the Admin API lists, reads and updates keys but does not create them | spend on the account | **Manual.** |
| AcoustID, Asana, Spotify | `ACOUSTID_API_KEY`, `ASANA_ACCESS_TOKEN`, `SPOTIPY_CLIENT_SECRET` (tier 2) | none: each is regenerated in the provider's console | use the app's quota; read and write the workspace; act as the Spotify app | **Manual.** `SPOTIPY_REFRESH_TOKEN` already renews itself through the API's re-auth link once the client secret is rotated. |
| Discord webhooks | `DISCORD_WEBHOOK_URL` and its `_DEFAULT`, `_ERRORS`, `_ACTIVITY`, `_RUNS` variants (tier 3) | a bot holding Manage Webhooks in the channels | post to one channel | **Manual.** The bot could post anywhere it sits and manage every webhook there. |
| Healthchecks.io | `HEALTHCHECKS_URL_WATCHER`, `HEALTHCHECKS_URL_DB_BACKUP` (tier 3) | a project API key, to create a check with a new ping URL | ping one check green | **Manual.** A project key can create, pause and delete every check. |

## What would change a verdict

- A provider that lets a credential renew only itself (Cloudflare does not:
  rolling needs API Tokens: Edit) would pass the test for that secret.
- A scoped admin permission narrower than the secret, for example a
  Turnstile permission limited to rotating one widget's secret, would pass.

Re-assess when an entry is added to the inventory as `kind: manual`, or when
a provider's API changes.
