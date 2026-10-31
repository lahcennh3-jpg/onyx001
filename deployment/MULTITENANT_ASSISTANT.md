# Multi-tenant AI assistant

Onyx already provides workspace provisioning, assistant chat, document permissions,
and group-based access controls. Use those features together rather than creating
a second identity or chat system.

Each workspace has a Postgres schema. Requests, search queries, cache entries,
background jobs, and streamed responses carry the workspace context.
Multi-tenant mode uses the EE application. Keep its existing edition and tier checks.

## Run locally

Use a separate development database. Do not turn an existing single-tenant database
into a multi-tenant database. The default schema contains the workspace catalog,
not assistant data.

From the repository root:

```sh
cd deployment/docker_compose
```

Create `.env` from `env.template` if you do not already have one. Keep existing
values. Set these development values:

```dotenv
MULTI_TENANT=true
AUTH_BACKEND=redis
DEV_MODE=true
REQUIRE_EMAIL_VERIFICATION=false
DISABLE_TELEMETRY=true
AUTO_PROVISION_DEFAULT_LLM_PROVIDERS=false
COMPOSE_PROFILES=s3-filestore
S3_ENDPOINT_URL=http://object-store:8333
MINIO_REPLICAS=0
```

Set `USER_AUTH_SECRET` and `ENCRYPTION_KEY_SECRET` to separate, randomly generated
secrets. Keep `.env` private and out of Git. Reuse these values after a restart.

Build the current source with the multi-tenant and Ollama overlays:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  -f docker-compose.multitenant.yml -f docker-compose.ollama.yml \
  up -d --build --wait
```

The overlays pull `qwen3:0.6b` and run catalog migrations before the API starts.
New signups create their own tenant schemas with Ollama as the default provider.
Ollama's model cache uses a persistent volume. Its unauthenticated API has no
published host port. The Onyx model servers supply embeddings for document search.

`qwen3:0.6b` is a small CPU model for development checks. Set `OLLAMA_DEFAULT_MODEL`
to another Ollama model for your workload. The initial download needs network
access to the Ollama registry and its model storage hosts.

Check `/api/health` through the web server. Then open the application using your
configured web domain. The default local web port is 3000.

This overlay disables email verification and uses development mode. It is for local
development. Production needs email verification, SMTP, HTTPS, private backing
services, persistent signing and encryption keys, and the cloud control plane.
Set `DEV_MODE=false` and `INTEGRATION_TESTS_MODE=false` in production. Do not
deploy the development overlay as a public SaaS service.

## Configure Ollama

The Ollama overlay enables automatic model provisioning for new workspaces.
It sets `OLLAMA_DEFAULT_API_BASE=http://ollama:11434` and
`OLLAMA_DEFAULT_MODEL=qwen3:0.6b`. Both values must be set for automatic provisioning.
`AUTO_PROVISION_DEFAULT_LLM_PROVIDERS=false` disables it. If hosted default keys are
also configured, those providers are created too; Ollama remains the chat default.

For an existing Ollama server, omit `docker-compose.ollama.yml`. Set the two
`OLLAMA_DEFAULT_*` variables to your reachable endpoint and model in `.env`, and
set `AUTO_PROVISION_DEFAULT_LLM_PROVIDERS=true`. Load the model on that server first.
These defaults apply during tenant provisioning; they do not change existing tenants.

For an existing workspace, open **Admin Panel → AI Models → Language Models**.
Connect **Ollama → Self-hosted Ollama**, enter the reachable base URL, refresh models,
and select the default chat model. A local Ollama server needs no hosted API key.

## Use another OpenAI-compatible provider

Sign up as the first user in a workspace. That user becomes its admin.

Open **Admin Panel → AI Models → Language Models**. Under **Self-hosted & Custom**,
connect **OpenAI-Compatible**. Enter the endpoint's base URL and API key. Refresh
the models, select a model, connect the provider, and set the default chat model.

Use a provider key entered through secure settings. Never place it in source files
or browser code. Allow the provider hostname in the environment's network settings.
For a local endpoint, use an address reachable from the API container.

Provider configuration belongs to each workspace. Configure the second workspace
separately. Without the Ollama overlay, the auto-provisioning flag above keeps
signups from receiving an operator's shared model credentials.

## Check the assistant workflow

1. Register two accounts in separate browser profiles. Each becomes its workspace's admin.
2. Ask a question in each workspace and reload the saved conversation. The Ollama overlay supplies the default model.
3. Copy the first workspace's chat URL into the second profile. It must not show that conversation.
4. Invite a new member from the first workspace. The member can create chats but cannot manage users.
5. Add a private knowledge source and an assistant that uses it. Check its answers and citations.
6. Ask the second workspace about that private content. It must not retrieve or cite those documents.

Model generation requires a working provider endpoint and credentials. Document
retrieval also requires a working embedding model and background workers.

## Automated isolation checks

Install the pinned Python dependencies from the repository root:

```sh
uv sync --frozen --no-default-groups --group backend --group dev --group ee
```

Run the focused checks:

```sh
uv run --no-sync pytest -q \
  backend/tests/unit/ee/onyx/server/middleware/test_tenant_tracking.py \
  backend/tests/unit/onyx/auth/test_redis_strategy_tenant_binding.py
```

The API integration test uses real Postgres, Redis, and OpenSearch. Start the
infrastructure using the local development ports, then run from the repository root:

```sh
MULTI_TENANT=true DEV_MODE=true REQUIRE_EMAIL_VERIFICATION=false \
DISABLE_TELEMETRY=true DISABLE_MODEL_SERVER=true SKIP_RESET=true \
POSTGRES_HOST=127.0.0.1 POSTGRES_PASSWORD=password \
DB_READONLY_USER=db_readonly_user DB_READONLY_PASSWORD=password \
REDIS_HOST=127.0.0.1 OPENSEARCH_HOST=127.0.0.1 \
uv run --no-sync pytest -q \
  backend/tests/integration/multitenant_tests/tenants/test_assistant_isolation.py
```

These are development database credentials. Use a disposable test database. The
suite provisions fresh workspaces and users. It verifies private chat isolation,
session precedence, member permissions, and authentication requirements. It does
not call a model provider or validate generated answers.

Requests with signed-in sessions ignore anonymous workspace cookies. A Redis
session is accepted only in the workspace that issued it. Malformed workspace
claims fail closed. Concurrent streams retain their own workspace until completion.
