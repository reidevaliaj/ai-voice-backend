# Multi-Tenant Rollout Notes

## What Changed
- Added a Postgres-backed multi-tenant control plane to the backend.
- Replaced hardcoded business config with tenant config loaded from the database.
- Added per-tenant phone routing, agent config versioning, encrypted integrations, call event logging, and call config snapshots.
- Added an internal FastAPI admin dashboard for creating tenants, assigning numbers, editing prompts/settings, and managing integrations.
- Added `/agent/session-config` so the shared LiveKit agent can load a per-call config snapshot.
- Changed `/incoming-call` to resolve the tenant from the inbound number and send tenant context toward the SIP leg.

## New Backend Files
- `db.py`, `models.py`, `security.py`, `app_config.py`
- `services/tenants.py`, `services/bootstrap.py`, `services/call_events.py`
- `routes/admin.py`, `routes/agent.py`
- `alembic/` migration setup and initial migration
- `templates/admin/` dashboard templates
- `requirements.txt`

## New Environment Variables
- `DATABASE_URL`
- `SESSION_SECRET_KEY`
- `PLATFORM_ENCRYPTION_KEY`
- `ADMIN_BOOTSTRAP_EMAIL`
- `ADMIN_BOOTSTRAP_PASSWORD`
- `DEFAULT_TENANT_SLUG`
- `DEFAULT_INBOUND_PHONE_NUMBER`
- `LIVEKIT_SIP_URI`
- `LIVEKIT_SIP_USERNAME`
- `LIVEKIT_SIP_PASSWORD`
- `PUBLIC_BASE_URL`
- `INTERNAL_API_KEY`

## Database Notes
- This rollout expects a brand-new database named `ai_voice_assistant`.
- Do not point `DATABASE_URL` at any of the unrelated VPS databases.
- Run Alembic migrations before starting Gunicorn on the new code.
- On startup, the backend bootstraps:
  - the admin user from env
  - the seed tenant `codestudio`
  - seed integrations from legacy env values if they exist

## Dashboard Notes
- Login path: `/admin/login`
- Bootstrap admin is created from `ADMIN_BOOTSTRAP_EMAIL` and `ADMIN_BOOTSTRAP_PASSWORD`
- Saving tenant config creates a new config version instead of mutating the old one.
- Integration credentials are stored encrypted in `tenant_integrations.credentials_encrypted`.

## Deployment Notes
1. Backup current `.env` in `~/apps/ai-voice-assistant`.
2. Create the new Postgres database and role for this app only.
3. Update `.env` with the new DB URL and secrets.
4. Install dependencies from `requirements.txt` inside the backend venv.
5. Run `alembic upgrade head`.
6. Restart the backend Gunicorn process.
7. Deploy the matching `livekit-agent` changes and restart the agent worker.
8. Update the LiveKit SIP trunk to map `X-*` headers into participant attributes for best routing fidelity.

## Rollback Notes
- Backend rollback is safe because the new database is additive and isolated.
- To roll back:
  - reset the backend repo to the previous commit
  - restore the previous `.env`
  - point away from the new DB URL if needed
  - restart Gunicorn

## Current Fallback Behavior
- If the agent cannot fetch tenant session config from the backend, it falls back to the legacy Code Studio defaults.
- If inbound number routing does not find a tenant, the backend falls back to `DEFAULT_TENANT_SLUG` if configured.
