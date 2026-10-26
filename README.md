# LaTrobe Irrigation System

An offline-first field operations platform and asynchronous operations backend ("Brain API") built for **LaTrobe Irrigation**.

The system consists of two primary components:
1. **Field Logger PWA (`frontend/`)**: A high-contrast, offline-first React + Vite Progressive Web App designed for field operators working in zero-connectivity irrigation zones. Uses Firebase Authentication and Cloud Firestore with multi-tab IndexedDB persistence.
2. **Operations Brain API & MCP Server (`backend/`)**: A Python FastAPI backend and FastMCP tool server that connects Cloud Firestore, QuickBooks Online (OAuth2 + Draft Invoice generation), and Telegram Bot operational alerts (low Ute inventory stock & draft invoice review links).

---

## Architecture & Key Features

- **Offline-First Field Logging**:
  - 5-step high-contrast wizard (`Step1Project` → `Step2Time` → `Step3Materials` → `Step4Evidence` → `Step5Review`).
  - Automatic local caching via Workbox Service Workers (`vite-plugin-pwa`) and Firestore IndexedDB multi-tab persistence.
- **Automated Ute Inventory & Low-Stock Alerts**:
  - Transactional stock deductions on `utes/{ute_id}/inventory/{item_id}` when shifts are logged.
  - Real-time Telegram notifications to operations managers whenever vehicle stock drops at or below its `reorder_threshold`.
- **End-of-Project QuickBooks Billing**:
  - Aggregates unbilled shifts, calculates labor hours by category and consumed materials, and generates a **DRAFT** invoice (`EmailStatus="NeedToSend"`) in QuickBooks Online without auto-emailing the client.
  - Marks shifts as invoiced in Firestore and sends the manager a direct QuickBooks review link via Telegram.
- **Model Context Protocol (MCP) Server**:
  - Built-in FastMCP server (`app/mcp/server.py`) exposing `get_project_work_summary`, `trigger_draft_invoice`, and `check_ute_inventory` for AI agent workflows.

---

## Repository Structure

```text
latrobe-system/
├── backend/                  # FastAPI Operations Brain & MCP Server
│   ├── app/
│   │   ├── api/v1/           # REST endpoints: /shifts, /projects, /inventory
│   │   ├── core/             # Settings (config.py) & Firebase RBAC auth (security.py)
│   │   ├── mcp/              # FastMCP server tools (server.py)
│   │   ├── schemas/          # Pydantic v2 models (shift, project, inventory, invoice)
│   │   ├── services/         # Firestore, QuickBooks Online, and Telegram clients
│   │   └── main.py           # FastAPI application entrypoint & /health check
│   ├── .env.example          # Backend environment template
│   └── requirements.txt      # Python dependencies
└── frontend/                 # React 18 + Vite + Tailwind CSS Offline-First PWA
    ├── src/
    │   ├── components/       # High-contrast UI components (FatButton, Stepper, QuickChip)
    │   ├── context/          # Firebase AuthContext
    │   ├── lib/              # Firebase client & multi-tab IndexedDB initialization
    │   └── pages/            # Login, Dashboard, and 5-step WizardFlow
    ├── .env.example          # Frontend Vite environment template
    ├── package.json          # Node dependencies & scripts
    └── vite.config.js        # Vite & Workbox PWA configuration
```

---

## Prerequisites

- **Python**: `3.11+` (recommended for backend)
- **Node.js**: `18+` or `20+` and `npm` (for frontend PWA)
- **Google Cloud / Firebase Project** with:
  - **Firebase Authentication** enabled (Email/Password provider)
  - **Cloud Firestore** enabled (Native mode)
- **Google Cloud SDK (`gcloud`)** & **Firebase CLI (`firebase-tools`)** (for GCP deployment)
- *(Optional for local dev, required for billing/alerts)*:
  - **Intuit QuickBooks Online** Developer App credentials (`Client ID`, `Client Secret`, `Realm ID`, OAuth2 tokens)
  - **Telegram Bot** token (via `@BotFather`) and Admin Chat ID

---

## Local Development Guide

### 1. Firebase Setup (Shared by Frontend & Backend)

1. Go to the [Firebase Console](https://console.firebase.google.com/) and select or create your project.
2. Enable **Authentication** → **Sign-in method** → **Email/Password**.
3. Enable **Firestore Database** in Native mode.
4. **Web App Config (Frontend)**:
   - Go to **Project Settings** → **General** → **Your apps** → Add a Web App.
   - Copy the Firebase configuration values (`apiKey`, `authDomain`, `projectId`, etc.) for `frontend/.env`.
5. **Service Account Key (Backend Local Dev)**:
   - Go to **Project Settings** → **Service accounts** → **Generate new private key**.
   - Save the downloaded JSON file as `backend/serviceAccountKey.json` (or configure Application Default Credentials via `gcloud auth application-default login`).
6. **Role-Based Access Control (RBAC)**:
   - The backend inspects Firebase ID token custom claims (`role`: `"operator" | "manager" | "admin"`). By default, authenticated users without custom claims are treated as `"operator"`.
   - Endpoints like `POST /api/v1/projects/{id}/close` and `POST /api/v1/inventory/utes/{ute_id}/alerts` require `"manager"` or `"admin"` roles.

---

### 2. Running the Backend Locally

1. **Create and activate a virtual environment**:
   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install Python dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure environment variables**:
   ```bash
   cp .env.example .env
   ```
   Open `backend/.env` and populate your credentials:
   ```dotenv
   ENVIRONMENT=development
   CORS_ORIGINS=["http://localhost:3000","http://localhost:5173","http://127.0.0.1:3000","http://127.0.0.1:5173"]

   # Path to your Firebase service account JSON key
   FIREBASE_CREDENTIALS_PATH=credentials/serviceAccountKey.json

   # QuickBooks Online OAuth2 & API
   QUICKBOOKS_CLIENT_ID=your_quickbooks_client_id
   QUICKBOOKS_CLIENT_SECRET=your_quickbooks_client_secret
   QUICKBOOKS_REDIRECT_URI=http://localhost:8000/api/v1/quickbooks/callback
   QUICKBOOKS_REALM_ID=your_quickbooks_realm_id
   QUICKBOOKS_ACCESS_TOKEN=your_initial_access_token
   QUICKBOOKS_REFRESH_TOKEN=your_initial_refresh_token
   QUICKBOOKS_ENVIRONMENT=sandbox

   # Telegram Bot Alerts
   TELEGRAM_BOT_TOKEN=123456789:ABCDEF_your_telegram_bot_token
   TELEGRAM_ADMIN_CHAT_ID=-1001234567890
   ```

4. **Start the FastAPI server**:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```
   - **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)
   - **Interactive Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
   - **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

5. **Running the MCP Server (Optional)**:
   To run the Model Context Protocol server for LLM/Agent integration:
   ```bash
   cd backend
   source .venv/bin/activate
   python -m app.mcp.server
   ```

---

### 3. Running the Frontend PWA Locally

1. **Install Node dependencies**:
   ```bash
   cd frontend
   npm install
   ```

2. **Configure environment variables**:
   ```bash
   cp .env.example .env
   ```
   Open `frontend/.env` and fill in your Firebase Web App credentials:
   ```dotenv
   VITE_FIREBASE_API_KEY=your_api_key_here
   VITE_FIREBASE_AUTH_DOMAIN=your_project_id.firebaseapp.com
   VITE_FIREBASE_PROJECT_ID=your_project_id
   VITE_FIREBASE_STORAGE_BUCKET=your_project_id.appspot.com
   VITE_FIREBASE_MESSAGING_SENDER_ID=your_messaging_sender_id
   VITE_FIREBASE_APP_ID=your_app_id
   ```

3. **Start the Vite development server**:
   ```bash
   npm run dev
   ```
   The app will be available at [http://localhost:5173](http://localhost:5173).

4. **Testing Production PWA & Service Worker Offline Mode Locally**:
   ```bash
   npm run build
   npm run preview
   ```
   Open the preview URL (default [http://localhost:4173](http://localhost:4173)) in Chrome DevTools → **Application** → **Service Workers** / **Network (Offline)** to verify zero-signal startup and IndexedDB shift queuing.

---

## Deploying to Google Cloud Platform (GCP)

The recommended production architecture on GCP uses:
- **Cloud Run** for the FastAPI Backend (`backend/`), leveraging **Application Default Credentials (ADC)** and **Secret Manager**.
- **Firebase Hosting** (backed by Google Cloud CDN) or **Cloud Run** for the React PWA (`frontend/`).
- **Cloud Firestore** for real-time/offline-synced storage (`projects`, `shifts`, `shift_logs`, `utes/{ute_id}/inventory`, `integrations/quickbooks`).

### Step 1: Initialize GCP Project & Enable APIs

```bash
export PROJECT_ID="latrobe-projectmanager"
export REGION="australia-southeast2"

gcloud config set project $PROJECT_ID

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com
```

### Step 2: Configure Service Account & IAM Permissions

 The backend (`app/core/security.py`) automatically falls back to **Application Default Credentials (ADC)** when `serviceAccountKey.json` is not present. On Cloud Run, this means you **do not** need to bundle a service account key file into your container.

1. **Create a dedicated Cloud Run service account**:
   ```bash
   gcloud iam service-accounts create latrobe-brain-sa \
     --display-name="LaTrobe Irrigation Brain API Service Account"
   ```

2. **Grant Firestore, Firebase Auth, and Secret Manager roles**:
   ```bash
   # Read/write access to Cloud Firestore
   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:latrobe-brain-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/datastore.user"

   # Verify Firebase Auth ID tokens
   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:latrobe-brain-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/firebaseauth.admin"

   # Access secrets in Google Secret Manager
   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:latrobe-brain-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

### Step 3: Store Sensitive Credentials in Secret Manager

Store QuickBooks and Telegram credentials in Google Cloud Secret Manager:

```bash
printf "your_quickbooks_client_id" | gcloud secrets create QUICKBOOKS_CLIENT_ID --data-file=-
printf "your_quickbooks_client_secret" | gcloud secrets create QUICKBOOKS_CLIENT_SECRET --data-file=-
printf "your_quickbooks_realm_id" | gcloud secrets create QUICKBOOKS_REALM_ID --data-file=-
printf "your_initial_refresh_token" | gcloud secrets create QUICKBOOKS_REFRESH_TOKEN --data-file=-
printf "" | gcloud secrets create TELEGRAM_BOT_TOKEN --data-file=-
printf "" | gcloud secrets create TELEGRAM_ADMIN_CHAT_ID --data-file=-
printf "" | gcloud secrets create TELEGRAM_WEBHOOK_SECRET --data-file=-
printf "https://latrobe-brain-api-339213924120.australia-southeast2.run.app" | gcloud secrets create TELEGRAM_WEBHOOK_BASE_URL --data-file=-
printf "" | gcloud secrets create GEMINI_API_KEY --data-file=-
printf "gemini-3.8-flash" | gcloud secrets create LLM_MODEL --data-file=-
```

*(Note: Once the backend refreshes QuickBooks OAuth2 tokens, it automatically persists updated tokens to Firestore under `integrations/quickbooks`.)*

### Step 4: Deploy the Backend to Cloud Run

1. **Deploy to Cloud Run from source**:
   ```bash
   gcloud run deploy latrobe-brain-api \
     --source ./backend \
     --region australia-southeast2 \
     --service-account="latrobe-brain-sa@latrobe-projectmanager.iam.gserviceaccount.com" \
     --allow-unauthenticated \
     --set-env-vars="ENVIRONMENT=production,QUICKBOOKS_ENVIRONMENT=production" \
     --set-env-vars='^;^CORS_ORIGINS=["https://app.latrobeirrigation.com.au","https://'"latrobe-projectmanager"'.web.app"]' \
     --set-secrets="QUICKBOOKS_CLIENT_ID=QUICKBOOKS_CLIENT_ID:latest,QUICKBOOKS_CLIENT_SECRET=QUICKBOOKS_CLIENT_SECRET:latest,QUICKBOOKS_REALM_ID=QUICKBOOKS_REALM_ID:latest,QUICKBOOKS_REFRESH_TOKEN=QUICKBOOKS_REFRESH_TOKEN:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_ADMIN_CHAT_ID=TELEGRAM_ADMIN_CHAT_ID:latest,TELEGRAM_WEBHOOK_SECRET=TELEGRAM_WEBHOOK_SECRET:latest,TELEGRAM_WEBHOOK_BASE_URL=TELEGRAM_WEBHOOK_BASE_URL:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,LLM_MODEL=LLM_MODEL:latest, "
   ```

   gcloud run deploy latrobe-brain-api \
         --source ./backend \
         --region australia-southeast2 \
         --service-account="latrobe-brain-sa@latrobe-projectmanager.iam.gserviceaccount.com" \
         --allow-unauthenticated \
         --set-env-vars="^;^ENVIRONMENT=production;QUICKBOOKS_ENVIRONMENT=production;CORS_ORIGINS=[\"https://app.latrobeirrigation.com.au\",\"https://latrobe-projectmanager.web.app\"]" \
         --set-secrets="QUICKBOOKS_CLIENT_ID=QUICKBOOKS_CLIENT_ID:latest,QUICKBOOKS_CLIENT_SECRET=QUICKBOOKS_CLIENT_SECRET:latest,QUICKBOOKS_REALM_ID=QUICKBOOKS_REALM_ID:latest,
  QUICKBOOKS_REFRESH_TOKEN=QUICKBOOKS_REFRESH_TOKEN:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_ADMIN_CHAT_ID=TELEGRAM_ADMIN_CHAT_ID:latest,
  TELEGRAM_WEBHOOK_SECRET=TELEGRAM_WEBHOOK_SECRET:latest,TELEGRAM_WEBHOOK_BASE_URL=TELEGRAM_WEBHOOK_BASE_URL:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,LLM_MODEL=LLM_MODEL:latest,
  /app/credentials/googleCalendarKey.json=GOOGLE_CALENDAR_KEY:latest"


3. **Verify Backend Deployment**:
   ```bash
   SERVICE_URL=$(gcloud run services describe latrobe-brain-api --region $REGION --format='value(status.url)')
   curl "${SERVICE_URL}/health"
   ```
   Confirm that `"status": "healthy"` and `"firebase_initialized": true` are returned.

---

### Step 5: Deploy the Frontend PWA

#### Deploy to Firebase Hosting (Recommended for PWAs)

Firebase Hosting provides global CDN caching, automatic SSL, and seamless SPA routing for the offline-first PWA.

1. **Build the production bundle**:
   ```bash
   cd frontend
   # Ensure frontend/.env has your production VITE_FIREBASE_* values
   npm install
   npm run build
   ```

2. **Initialize and deploy with Firebase CLI**:
   ```bash
   npm install -g firebase-tools
   firebase login
   firebase init hosting
   ```
   When prompted by `firebase init hosting`:
   - **Use an existing project**: Select your GCP/Firebase project (`$PROJECT_ID`).
   - **Public directory**: `dist`
   - **Configure as a single-page app (rewrite all urls to /index.html)**: `Yes`
   - **Set up automatic builds and deploys with GitHub**: `No` (or `Yes` if desired)
   - **Overwrite `dist/index.html`**: `No`

3. **Deploy**:
   ```bash
   firebase deploy --only hosting
   ```

---

## API & MCP Reference Summary

### REST API Endpoints (`backend/app/api/v1`)

All `/api/v1/*` endpoints require a `Bearer <Firebase_ID_Token>` header.

| Method | Path | Role Required | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Public | Returns service health and Firebase Admin readiness. |
| `POST` | `/api/v1/shifts` | `operator`+ | Logs a field shift in Firestore, deducts Ute stock transactionally, and sends low-stock Telegram alerts. |
| `POST` | `/api/v1/projects/{id}/close` | `manager` / `admin` | Aggregates unbilled shifts, creates a QuickBooks Online DRAFT invoice, marks shifts invoiced, closes the project, and alerts the manager on Telegram. |
| `GET` | `/api/v1/inventory/utes/{ute_id}` | `operator`+ | Returns current stock levels and low-stock alerts for a Ute. |
| `POST` | `/api/v1/inventory/utes/{ute_id}/deduct` | `operator`+ | Manually deducts consumed items from a Ute's inventory. |
| `POST` | `/api/v1/inventory/utes/{ute_id}/alerts` | `manager` / `admin` | Evaluates a Ute's inventory and dispatches a Telegram low-stock alert. |

### FastMCP Tools (`backend/app/mcp/server.py`)

- `get_project_work_summary(project_id: str)`: Summarizes total/billed/unbilled labor hours by category, consumed parts, and chronological operator notes.
- `trigger_draft_invoice(project_id: str)`: Executes the end-of-project QuickBooks draft invoicing and Telegram alert workflow.
- `check_ute_inventory(ute_id: str)`: Inspects vehicle stock and highlights items at or below reorder thresholds.






curl -X POST "https://api.telegram.org/bot8986649682:AAHu_L05mvNwFX7C5CsvbRmHiqa6DNx8uS0/setWebhook?url=https://latrobe-brain-api-339213924120.australia-southeast2.run.app/webhook/telegram/bmtFoTNmQly58imx5DgZLGxJQgHEO1SnP3jbqv86qcY"