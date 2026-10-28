# LaTrobe Irrigation System

An offline-first field operations platform and AI-powered operations & accounting backend ("Brain API") built for **LaTrobe Irrigation**.

The system consists of two primary components:
1. **Field Logger PWA (`frontend/`)**: A high-contrast, offline-first React + Vite Progressive Web App designed for field operators working in low/zero-connectivity irrigation zones. Uses Firebase Authentication, GPS proximity project sorting, and local IndexedDB offline shift & material queuing.
2. **Operations Brain API (`backend/`)**: A Python FastAPI backend that connects Cloud Firestore, QuickBooks Online (OAuth2 + Customers/Projects/Items/Vendors Sync + Draft Invoices & Supplier Bills), Google Calendar, Gmail (IMAP/SMTP), Gemini AI (`gemini-3-flash-preview` / `gemini-3.8-flash`), and an interactive Telegram Operations Bot.

---

## Architecture & Key Features

### 1. Offline-First Field Logging PWA (`frontend/`)
- **5-Step High-Contrast Wizard**:
  - `Step1Project` (GPS-sorted active projects) → `Step2Time` (hours & category) → `Step3Materials` (Top 6 quick-picks, scrollable catalog, & inline custom material creation) → `Step4Evidence` (field notes) → `Step5Review`.
- **Operator Attribution**:
  - Every logged shift automatically records who logged it and when (`logged_by`, `operator_name`, `operator_email`, `operator_id`) in the Firestore `shifts` collection.
- **Offline Queue & Sync**:
  - Automatic local caching via Workbox Service Workers (`vite-plugin-pwa`) and offline shift/material queue synchronization when connectivity returns.

### 2. Autonomous ReAct Telegram Operations Agent (`app/services/agent_runner.py`)
- **Real-Time Operational Queries**:
  - Powered by Gemini with low-thinking latency optimization (`thinkingLevel: "LOW"`), a 60-second in-memory Firestore cache, persistent HTTP connection pooling, pre-computed 14-day `Australia/Melbourne` calendar context, and 15-minute rolling conversational memory per `chat_id`.
- **Registered Agent Tools**:
  - `get_project_status`: Comprehensive project summary (client, location, total/unbilled hours by category and by operator, individual shift logs, materials used, and field notes).
  - `get_shifts_activity`: Queries `shifts` across projects or operators to track who did what and when.
  - `search_materials`: Searches master inventory (`materials` and `catalog_items`).
  - `list_active_projects`: Lists all active projects in Firestore.
  - `get_calendar_events` & `book_calendar_job`: Checks availability and schedules jobs in Google Calendar.

### 3. Human-in-the-Loop Customer Booking Copilot (`app/services/booking_service.py`)
- **2-Minute Gmail Polling & Pre-Flight Privacy Filter**:
  - Polls Gmail INBOX via IMAP (`POST /api/v1/webhooks/poll-email`) every 2 minutes.
  - Header-only pre-flight filter (`is_blocked_personal_or_banking_email`) skips banking, government, security/OTP, and personal emails without downloading their bodies or marking them read in Gmail.
- **Customer Resolution & Conflict-Aware Scheduling**:
  - Matches senders against Firestore `customers`, checks Google Calendar and pending proposals (`booking_proposals`) to avoid double-booking, and sends an interactive Telegram Approval Card (`✅ Confirm & Notify`, `🤫 Confirm Silently`, `❌ Reject / Manual`).
- **Dual-Database Sync & Outbound Confirmation**:
  - Instant button deactivation on tap to prevent duplicate confirmations.
  - Creates the event in Google Calendar, writes the booking record to `projects/{project_id}/bookings/{booking_id}`, and (when `Confirm & Notify` is chosen) sends an outbound SMTP confirmation email to the client.

### 4. AI-Powered Supplier Invoice Extraction — Accounts Payable (`app/services/ap_invoice_service.py`)
- **Stage 1 — QBO Supplier Whitelist (`suppliers`)**:
  - Syncs all active QuickBooks Online Vendors into Firestore `suppliers` (`vendor_id`, `display_name`, `email_address`, `email_domain`).
- **Stage 2 — 3-Tier Gmail Filter**:
  - Filters incoming emails with PDF attachments before processing:
    1. **Tier 1 (Direct Match):** Exact sender email match against `suppliers.email_address`.
    2. **Tier 2 (Domain Match):** Sender domain match against `suppliers.email_domain` (excluding generic consumer domains like `@gmail.com`, `@bigpond.com`, etc.).
    3. **Tier 3 (Accounting Catch-All):** Sender domain from SaaS accounting platforms (`@post.xero.com`, `@apps.myob.com`, `@quickbooks.com`, etc.) verified via a fast Gemini triage check against Firestore supplier names.
- **Stage 3 — PDF Extraction via Gemini File API**:
  - Uploads the raw PDF attachment to the Gemini File API and extracts structured JSON (`supplier_name`, `invoice_number`, `issue_date`, `due_date`, `total_amount_inc_gst`, `gst_amount`, `purchase_order_ref`).
- **Stage 4 — Telegram Approval & QBO Bill Creation**:
  - Stores the extracted invoice in `pending_bills/{bill_id}` and sends a Telegram card with `[ ✅ Approve & Sync to QBO ]` and `[ ❌ Reject / Spam ]`.
  - On approval, creates the Bill in QuickBooks Online (`POST /v3/company/{realmId}/bill`) mapped to the supplier's `VendorRef`, deletes the document from `pending_bills`, and updates the Telegram card to `✅ Bill Synced to QBO`.

### 5. Automated QuickBooks Online Sync & End-of-Project Invoicing (`app/services/sync_service.py`)
- **30-Minute QBO Mirror Sync (`POST /api/v1/sync/qbo`)**:
  - Mirrors QBO Customers (`customers`), Sub-Customer Jobs (`projects` with automatic Nominatim geocoding of shipping/billing addresses), Items (`catalog_items`), and Vendors (`suppliers`) into Firestore.
- **Draft Invoice Generation**:
  - Aggregates unbilled shifts, calculates labor hours by category and consumed materials, generates a **DRAFT** invoice (`EmailStatus="NeedToSend"`) in QuickBooks Online, marks shifts as invoiced, and sends a review link to Telegram.

---

## Repository Structure

```text
latrobe-system/
├── backend/                          # FastAPI Operations Brain & MCP Server
│   ├── app/
│   │   ├── api/v1/                   # REST endpoints: /shifts, /projects, /inventory, /sync, /webhooks
│   │   ├── core/                     # Settings (config.py) & Firebase RBAC auth (security.py)
│   │   ├── mcp/                      # FastMCP server tools (server.py)
│   │   ├── schemas/                  # Pydantic v2 models (shift, project, inventory, invoice, booking, telegram)
│   │   ├── services/
│   │   │   ├── agent_runner.py       # Autonomous Gemini ReAct loop & tool dispatcher
│   │   │   ├── ap_invoice_service.py # 4-stage Supplier Invoice PDF extraction & QBO Bill approval
│   │   │   ├── booking_service.py    # Human-in-the-Loop customer booking copilot
│   │   │   ├── calendar_service.py   # Google Calendar availability & event booking
│   │   │   ├── email_service.py      # Gmail IMAP polling, pre-flight filter, & SMTP sender
│   │   │   ├── firestore_service.py  # Firestore CRUD & 60s in-memory TTL caches
│   │   │   ├── geocoding_service.py  # Nominatim address-to-coordinates geocoder
│   │   │   ├── quickbooks_service.py # QBO REST API v3 client (Customers, Items, Vendors, Invoices, Bills)
│   │   │   ├── sync_service.py       # Idempotent QBO -> Firestore batch sync
│   │   │   └── telegram_service.py   # Telegram Bot API client, webhook handler, & inline keyboards
│   │   └── main.py                   # FastAPI application entrypoint & /health check
│   ├── .env.example                  # Backend environment template
│   └── requirements.txt              # Python dependencies
└── frontend/                         # React 18 + Vite + Tailwind CSS Offline-First PWA
    ├── src/
    │   ├── components/               # High-contrast UI components (FatButton, Stepper, QuickChip)
    │   ├── context/                  # Firebase AuthContext
    │   ├── lib/                      # Firebase client & offline queue helpers (offlineQueue.js)
    │   └── pages/                    # Login, Dashboard, and 5-step WizardFlow
    ├── .env.example                  # Frontend Vite environment template
    ├── package.json                  # Node dependencies & scripts
    └── vite.config.js                # Vite & Workbox PWA configuration
```

---

## Local Development Guide

### 1. Running the Backend Locally

1. **Create and activate a virtual environment**:
   ```bash
   cd backend
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

2. **Configure `backend/.env`**:
   ```dotenv
   ENVIRONMENT=development
   CORS_ORIGINS=["http://localhost:3000","http://localhost:5173","https://app.latrobeirrigation.com.au"]

   # Firebase & Google Calendar Service Account Credentials
   FIREBASE_CREDENTIALS_PATH=credentials/serviceAccountKey.json
   GOOGLE_SERVICE_ACCOUNT_FILE=credentials/googleCalendarKey.json
   GOOGLE_CALENDAR_ID=your_calendar_email@gmail.com

   # QuickBooks Online OAuth2 & API
   QUICKBOOKS_CLIENT_ID=your_quickbooks_client_id
   QUICKBOOKS_CLIENT_SECRET=your_quickbooks_client_secret
   QUICKBOOKS_REDIRECT_URI=http://localhost:8000/api/v1/quickbooks/callback
   QUICKBOOKS_REALM_ID=your_quickbooks_realm_id
   QUICKBOOKS_ACCESS_TOKEN=your_initial_access_token
   QUICKBOOKS_REFRESH_TOKEN=your_initial_refresh_token
   QUICKBOOKS_ENVIRONMENT=production

   # Telegram Bot & Webhook
   TELEGRAM_BOT_TOKEN=123456789:ABCDEF_your_telegram_bot_token
   TELEGRAM_ADMIN_CHAT_ID=your_telegram_chat_id
   TELEGRAM_WEBHOOK_SECRET=your_webhook_secret
   TELEGRAM_WEBHOOK_BASE_URL=https://latrobe-brain-api-339213924120.australia-southeast2.run.app

   # Gemini LLM Configuration
   GEMINI_API_KEY=your_gemini_api_key
   LLM_MODEL=gemini-3-flash-preview

   # Gmail IMAP / SMTP Integration (Google App Password)
   GMAIL_ADDRESS=your_email@gmail.com
   GMAIL_APP_PASSWORD=your_16_char_app_password
   ```

3. **Start the FastAPI server**:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```
   - **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)
   - **Interactive Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)

---

### 2. Running the Frontend PWA Locally

```bash
cd frontend
npm install
npm run dev
```
The app will be available at [http://localhost:5173](http://localhost:5173).

---

## Deploying to Google Cloud Platform (GCP)

### 1. Deploy Backend to Cloud Run (`australia-southeast2`)

```bash
cd backend
gcloud run deploy latrobe-brain-api \
  --source . \
  --region australia-southeast2 \
  --project latrobe-projectmanager \
  --service-account="latrobe-brain-sa@latrobe-projectmanager.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --set-env-vars="^;^ENVIRONMENT=production;QUICKBOOKS_ENVIRONMENT=production;GMAIL_ADDRESS=marcoripa1512@gmail.com;CORS_ORIGINS=[\"https://app.latrobeirrigation.com.au\",\"https://latrobe-projectmanager.web.app\"]" \
  --set-secrets="QUICKBOOKS_CLIENT_ID=QUICKBOOKS_CLIENT_ID:latest,QUICKBOOKS_CLIENT_SECRET=QUICKBOOKS_CLIENT_SECRET:latest,QUICKBOOKS_REALM_ID=QUICKBOOKS_REALM_ID:latest,QUICKBOOKS_REFRESH_TOKEN=QUICKBOOKS_REFRESH_TOKEN:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_ADMIN_CHAT_ID=TELEGRAM_ADMIN_CHAT_ID:latest,TELEGRAM_WEBHOOK_SECRET=TELEGRAM_WEBHOOK_SECRET:latest,TELEGRAM_WEBHOOK_BASE_URL=TELEGRAM_WEBHOOK_BASE_URL:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,LLM_MODEL=LLM_MODEL:latest,GMAIL_APP_PASSWORD=GMAIL_APP_PASSWORD:latest,/app/credentials/googleCalendarKey.json=GOOGLE_CALENDAR_KEY:latest"
```

### 2. Cloud Scheduler Jobs (`australia-southeast1`)

- **QuickBooks 30-Minute Sync (`qbo-30min-sync`)**:
  - Schedule: `*/30 * * * *` (`Australia/Melbourne`)
  - Target: `POST https://latrobe-brain-api-339213924120.australia-southeast2.run.app/api/v1/sync/qbo`
- **Gmail 2-Minute Inbox Poll (`gmail-2min-poll`)**:
  - Schedule: `*/2 * * * *` (`Australia/Melbourne`)
  - Target: `POST https://latrobe-brain-api-339213924120.australia-southeast2.run.app/api/v1/webhooks/poll-email`

### 3. Deploy Frontend PWA to Firebase Hosting

```bash
cd frontend
npm run build
npx firebase deploy --only hosting --project latrobe-projectmanager
```

---

## REST API Reference Summary

| Method | Path | Auth / Role | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Public | Returns service health and Firebase Admin readiness. |
| `POST` | `/api/v1/shifts` | `operator`+ | Logs a field shift in Firestore (`shifts`) with operator attribution (`logged_by`, `operator_name`, `operator_email`, `operator_id`). |
| `GET` | `/api/v1/projects` | `operator`+ | Lists active projects in Firestore (with GPS coordinates for proximity sorting). |
| `POST` | `/api/v1/projects/{id}/close` | `manager` / `admin` | Aggregates unbilled shifts, creates a QuickBooks Online DRAFT invoice, marks shifts invoiced, closes the project, and alerts the manager on Telegram. |
| `GET` | `/api/v1/inventory/materials` | `operator`+ | Searches master inventory (`materials` and `catalog_items`). |
| `POST` | `/api/v1/inventory/materials` | `operator`+ | Creates a new material in Firestore `materials`. |
| `POST` | `/api/v1/sync/qbo` | Scheduler / `admin` | Syncs Customers (`customers`), Sub-Customer Jobs (`projects`), Items (`catalog_items`), and Vendors (`suppliers`) from QuickBooks Online. |
| `POST` | `/api/v1/webhooks/poll-email` | Scheduler / Secret | Polls Gmail IMAP for unread emails, routing PDF supplier invoices to the 4-stage AP pipeline and customer emails to the booking copilot. |
| `POST` | `/api/v1/webhooks/test-inbound` | Public / Test | Test harness endpoint for inbound customer booking messages (`InboundMessage`). |
| `POST` | `/webhook/telegram/{secret_token}` | Telegram Secret | Handles incoming Telegram messages (ReAct agent) and inline button callbacks (`confirm_notify`, `confirm_silent`, `reject`, `approve_bill`, `reject_bill`). |
