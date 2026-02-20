# prototype-creator (Figma Proxy API)

## What this is
Small FastAPI service that securely proxies selected Figma API endpoints so your Custom GPT can call them via Actions.

## Endpoints
- `GET /`
- `GET /health`
- `GET /sharepoint/dci-architecture-plan`
- `GET /figma/files/{file_key}`
- `GET /figma/files/{file_key}/nodes?ids=...&depth=...`
- `GET /figma/files/{file_key}/images?ids=...&format=png&scale=1`

## Auth model
- Calls to `/figma/*` and `/sharepoint/*` require header: `X-Service-Key: <SERVICE_KEY>` if `SERVICE_KEY` is set.
- Figma auth uses `FIGMA_TOKEN` (server-side, never exposed to GPT).
- SharePoint auth uses Microsoft Graph app credentials (server-side, never exposed to GPT).

## Local run
1. `cd /Users/TBURKE/prototype-creator`
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. `cp .env.example .env` and set real values
5. `uvicorn app:app --reload --port 8000`
6. Open docs: `http://localhost:8000/docs`

## Deploy to Render
1. Push this repo to your private GitHub account (`ilike2mtb`).
2. In Render: New + > Blueprint.
3. Select this repo and use `render.yaml`.
4. In Render environment variables, set:
   - `FIGMA_TOKEN`
   - `SERVICE_KEY`
   - `PUBLIC_BASE_URL` (for this app: `https://prototype-creator.onrender.com`)
   - `MS_TENANT_ID`
   - `MS_CLIENT_ID`
   - `MS_CLIENT_SECRET`
   - `SP_SITE_ID`
   - `SP_DRIVE_ID`
   - `SP_FILE_PATH` (example: `Shared Documents/DCI Architecture Plan.xlsx`)
   - `SP_WORKSHEET_NAME` (optional; if empty, first worksheet is used)
5. Deploy. OpenAPI schema URL:
   - `https://<your-render-domain>/openapi.json`

## Add to Custom GPT Actions
1. In GPT Builder, add an Action.
2. Use OpenAPI URL:
   - `https://<your-render-domain>/openapi.json`
3. Set authentication to **API Key**:
   - Header name: `X-Service-Key`
   - Value: same as your `SERVICE_KEY` in Render.
4. Save the action and test with:
   - `GET /health` for connectivity
   - `GET /sharepoint/dci-architecture-plan` for dynamic spreadsheet data
   - `GET /figma/files/{file_key}` for Figma calls
