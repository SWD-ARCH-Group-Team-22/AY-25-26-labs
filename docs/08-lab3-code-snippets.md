# Lab 3 — Code Snippets

This file is the code companion to [08-lab3-step-by-step.md](08-lab3-step-by-step.md). It contains the complete instrumented worker and all supporting files.

---

## Step 1 — Start Jaeger

Jaeger is already defined in `docker-compose.yml`. If you started the infrastructure without it, restart including the `jaeger` service:

**macOS / Linux / WSL:**

```sh
docker compose up database messagebus cache jaeger
```

**Windows PowerShell:**

```powershell
docker compose up database messagebus cache jaeger
```

Jaeger UI: `http://localhost:16686`
OTLP HTTP endpoint (used by the worker): `http://localhost:4318`

---

## Step 2 — Create the project folder

Start from a copy of `lab2-worker-rest/` or create a new folder:

**macOS / Linux / WSL:**

```sh
cp -r lab2-worker-rest lab3-worker-observable
cd lab3-worker-observable
```

**Windows PowerShell:**

```powershell
Copy-Item -Recurse lab2-worker-rest lab3-worker-observable
cd lab3-worker-observable
```

---

## Step 3 — `requirements.txt`

```
requests==2.32.3
python-dotenv==1.0.1
structlog==24.4.0
prometheus-client==0.25.0
opentelemetry-sdk==1.30.0
opentelemetry-exporter-otlp-proto-http==1.30.0
opentelemetry-instrumentation-requests==0.51b0
opentelemetry-exporter-prometheus==0.51b0
```

---

## Step 4 — `.env`

```sh
MZINGA_URL=http://localhost:3000
MZINGA_EMAIL=<admin_email>
MZINGA_PASSWORD=<admin_password>
POLL_INTERVAL_SECONDS=5
SMTP_HOST=localhost
SMTP_PORT=1025
EMAIL_FROM=worker@mzinga.io
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=email-worker
PROMETHEUS_PORT=8000
```

---

## Step 5 — `worker.py`

```python
import os
import time
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
import requests
import structlog

# OpenTelemetry — tracing
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.instrumentation.requests import RequestsInstrumentor

# OpenTelemetry — metrics
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader  # <-- Updated import
from prometheus_client import start_http_server

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

MZINGA_URL = os.environ["MZINGA_URL"]
MZINGA_EMAIL = os.environ["MZINGA_EMAIL"]
MZINGA_PASSWORD = os.environ["MZINGA_PASSWORD"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", 5))
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", 1025))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
SERVICE_NAME_VALUE = os.getenv("OTEL_SERVICE_NAME", "email-worker")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", 8000))

# ── OpenTelemetry: Tracing ────────────────────────────────────────────────────

resource = Resource(attributes={
    SERVICE_NAME: SERVICE_NAME_VALUE,
    SERVICE_VERSION: "1.0.0",
})

tracer_provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces")
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(tracer_provider)

RequestsInstrumentor().instrument()

tracer = trace.get_tracer(SERVICE_NAME_VALUE)

# ── OpenTelemetry: Metrics ────────────────────────────────────────────────────

# 1. Start the HTTP server to expose the /metrics endpoint for Prometheus to scrape
start_http_server(port=PROMETHEUS_PORT)

# 2. Use PrometheusMetricReader (Pull-based) instead of PeriodicExportingMetricReader
metric_reader = PrometheusMetricReader()
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)

meter = metrics.get_meter(SERVICE_NAME_VALUE)

emails_processed = meter.create_counter(
    name="emails_processed_total",
    description="Total number of communications processed",
    unit="1",
)
processing_duration = meter.create_histogram(
    name="email_processing_duration_seconds",
    description="End-to-end duration of processing one communication",
    unit="s",
)
smtp_duration = meter.create_histogram(
    name="smtp_send_duration_seconds",
    description="Duration of the SMTP send call",
    unit="s",
)
poll_counter = meter.create_counter(
    name="worker_poll_total",
    description="Number of poll cycles",
    unit="1",
)

# ── Structured logging ────────────────────────────────────────────────────────

def add_otel_context(logger, method, event_dict):
    """Inject active trace_id and span_id into every log entry."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_otel_context,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger(service=SERVICE_NAME_VALUE)

# ── MZinga API helpers ────────────────────────────────────────────────────────

def login() -> str:
    resp = requests.post(
        f"{MZINGA_URL}/api/users/login",
        json={"email": MZINGA_EMAIL, "password": MZINGA_PASSWORD},
    )
    resp.raise_for_status()
    log.info("authenticated")
    return resp.json()["token"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_pending(token: str) -> list:
    resp = requests.get(
        f"{MZINGA_URL}/api/communications",
        params={"where[status][equals]": "pending", "depth": 1},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json().get("docs", [])


def fetch_doc(token: str, doc_id: str) -> dict:
    resp = requests.get(
        f"{MZINGA_URL}/api/communications/{doc_id}",
        params={"depth": 1},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


def update_status(token: str, doc_id: str, status: str):
    resp = requests.patch(
        f"{MZINGA_URL}/api/communications/{doc_id}",
        json={"status": status},
        headers=auth_headers(token),
    )
    resp.raise_for_status()

# ── Email helpers ─────────────────────────────────────────────────────────────

def slate_to_html(nodes: list) -> str:
    html = ""
    for node in nodes or []:
        if node.get("type") == "paragraph":
            html += f"<p>{slate_to_html(node.get('children', []))}</p>"
        elif node.get("type") == "h1":
            html += f"<h1>{slate_to_html(node.get('children', []))}</h1>"
        elif node.get("type") == "h2":
            html += f"<h2>{slate_to_html(node.get('children', []))}</h2>"
        elif node.get("type") == "ul":
            html += f"<ul>{slate_to_html(node.get('children', []))}</ul>"
        elif node.get("type") == "li":
            html += f"<li>{slate_to_html(node.get('children', []))}</li>"
        elif node.get("type") == "link":
            url = node.get("url", "#")
            html += f'<a href="{url}">{slate_to_html(node.get("children", []))}</a>'
        elif "text" in node:
            text = node["text"]
            if node.get("bold"):
                text = f"<strong>{text}</strong>"
            if node.get("italic"):
                text = f"<em>{text}</em>"
            html += text
        else:
            html += slate_to_html(node.get("children", []))
    return html


def extract_emails(relationship_list: list) -> list[str]:
    emails = []
    for r in relationship_list or []:
        value = r.get("value") or {}
        if isinstance(value, dict) and value.get("email"):
            emails.append(value["email"])
    return emails


def send_email(to_addresses: list[str], subject: str, html: str,
               cc_addresses: list[str] = None, bcc_addresses: list[str] = None):
    with tracer.start_as_current_span("send_email") as span:
        span.set_attribute("recipient_count", len(to_addresses))
        t0 = time.perf_counter()
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = ", ".join(to_addresses)
        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)
        msg.attach(MIMEText(html, "html"))
        all_recipients = to_addresses + (cc_addresses or []) + (bcc_addresses or [])
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())
        smtp_duration.record(time.perf_counter() - t0)

# ── Processing ────────────────────────────────────────────────────────────────

def process(token: str, doc: dict) -> str:
    doc_id = doc["id"]
    structlog.contextvars.bind_contextvars(doc_id=doc_id)

    with tracer.start_as_current_span("process_communication") as span:
        span.set_attribute("doc_id", doc_id)
        t0 = time.perf_counter()

        update_status(token, doc_id, "processing")
        log.info("processing_started")

        try:
            to_emails = extract_emails(doc.get("tos"))
            if not to_emails:
                raise ValueError("No valid 'to' email addresses found")
            cc_emails = extract_emails(doc.get("ccs"))
            bcc_emails = extract_emails(doc.get("bccs"))

            with tracer.start_as_current_span("serialize_body") as s:
                nodes = doc.get("body") or []
                s.set_attribute("node_count", len(nodes))
                html = slate_to_html(nodes)

            send_email(to_emails, doc["subject"], html, cc_emails, bcc_emails)
            update_status(token, doc_id, "sent")

            duration = time.perf_counter() - t0
            processing_duration.record(duration)
            emails_processed.add(1, {"status": "sent", "recipient_count": len(to_emails)})
            log.info("processing_completed", status="sent", duration_s=round(duration, 3))

        except Exception as e:
            span.set_status(trace.StatusCode.ERROR, str(e))
            span.record_exception(e)
            update_status(token, doc_id, "failed")
            emails_processed.add(1, {"status": "failed", "recipient_count": 0})
            log.error("processing_failed", error=str(e))

    structlog.contextvars.unbind_contextvars("doc_id")
    return token

# ── Poll loop ─────────────────────────────────────────────────────────────────

def poll():
    token = login()
    log.info("worker_started", poll_interval_s=POLL_INTERVAL, prometheus_port=PROMETHEUS_PORT)
    while True:
        try:
            docs = fetch_pending(token)
            if docs:
                poll_counter.add(1, {"result": "found"})
                for doc in docs:
                    token = process(token, doc)
            else:
                poll_counter.add(1, {"result": "empty"})
                time.sleep(POLL_INTERVAL)
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                log.warning("token_expired_reauthenticating")
                token = login()
            else:
                log.error("http_error", status_code=e.response.status_code, error=str(e))
                time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll()
```

---

## Step 6 — Install and run

**macOS / Linux / WSL:**

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python worker.py
```

**Windows PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python worker.py
```

---

## Step 7 — Verify metrics endpoint

**macOS / Linux / WSL:**

```sh
curl -s http://localhost:8000/metrics | grep emails_processed
```

**Windows PowerShell:**

```powershell
curl.exe -s http://localhost:8000/metrics | Select-String "emails_processed"
```

---

## Step 8 — Reset a document to `pending` for re-testing

```sh
mongosh "mongodb://admin:admin@localhost:27017/mzinga?authSource=admin&directConnection=true"
```

```js
use mzinga
db.communications.updateOne({ subject: "Test subject" }, { $set: { status: "pending" } })
```

---

## Step 9 — Simulate a failure to test error traces

Stop MailHog:

**macOS / Linux / WSL:**

```sh
docker stop $(docker ps -q --filter ancestor=mailhog/mailhog)
```

**Windows PowerShell:**

```powershell
docker stop $(docker ps -q --filter ancestor=mailhog/mailhog)
```

Create a Communication document. Observe the `send_email` span in Jaeger showing ERROR status, and `emails_processed_total{status="failed"}` incrementing in the metrics endpoint.

Restart MailHog:

```sh
docker run -d -p 1025:1025 -p 8025:8025 mailhog/mailhog
```

---

**Previous:** [08 — Lab 3 Step by Step](08-lab3-step-by-step.md)
