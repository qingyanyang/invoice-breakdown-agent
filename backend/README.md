# Backend

FastAPI API for Enterprise Knowledge Intelligence. Images publish to public GHCR as `knowledge-intelligence-api` and run on Azure Container Apps.

## Stack

- Python 3.12
- FastAPI + Uvicorn
- SQLAlchemy 2.x + psycopg (Supabase PostgreSQL / pgvector)
- Default schema: `knowledge_intelligence`

## Local setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit DATABASE_URL if you need DB connectivity locally
uvicorn app.main:app --reload --port 8000
```

Health check: `GET http://localhost:8000/health`

`DATABASE_URL` is optional for the MVP `/health` endpoint. Importing `app.database` requires it.

## Docker

```bash
docker build -t knowledge-intelligence-api:local .
docker run --rm -p 8000:8000 \
  -e ENVIRONMENT=demo \
  -e POSTGRES_SCHEMA=knowledge_intelligence \
  knowledge-intelligence-api:local
```

## Azure / CD

1. Push a **public** GHCR image via `backend-cd` (or local `docker push`).
2. Set `deployContainerApp = true` and `containerImage` in `infra/parameters/demo.bicepparam`.
3. Redeploy infra with `DATABASE_URL` (and optional `LLM_API_KEY`).
4. Set GitHub Environment variable `CONTAINER_APP_NAME` from infra outputs for ongoing image updates.
