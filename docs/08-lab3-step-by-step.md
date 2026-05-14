# Lab 3 — Step by Step: Observability with OpenTelemetry, Prometheus, and Structured Logging

**Goal:** Instrument the Python email worker from Lab 2 with the three pillars of observability — logs, metrics, and traces — using OpenTelemetry. Export metrics to Prometheus and traces to Jaeger. At the end of this lab you can observe what the worker is doing, how long each operation takes, and diagnose failures without reading source code.

The starting point is the Lab 2 REST API worker (`lab2-worker-rest/`). All instrumentation is added to that worker. No changes to MZinga are required.

---

## Concepts: the three pillars of observability

Before writing any code, understand what you are building and why.

### Logs

Logs are timestamped text records of discrete events. The worker already uses Python's `logging` module. The problem with plain text logs is that they are hard to query, correlate, and aggregate. **Structured logging** replaces free-form text with JSON objects where every field is a named key-value pair. A structured log entry for a sent email might look like:

```json
{"timestamp": "2025-01-15T10:23:01Z", "level": "INFO", "event": "email_sent", "doc_id": "abc123", "recipients": 3, "duration_ms": 142}
```

This can be indexed, filtered, and aggregated by any log management system (Loki, Elasticsearch, Datadog) without parsing.

### Metrics

Metrics are numeric measurements aggregated over time. They answer questions like "how many emails were sent in the last minute?" or "what is the 95th percentile processing time?". Prometheus is the standard metrics backend in the cloud-native ecosystem. It scrapes an HTTP endpoint (`/metrics`) on your service at a regular interval and stores the time series.

OpenTelemetry provides a metrics API that is backend-agnostic — you define counters, histograms, and gauges in your code, and the SDK exports them to Prometheus (or any other backend) without changing the instrumentation.

### Traces, spans, and the relationship between them

A **trace** represents the end-to-end journey of a single unit of work through a system. In this lab, one trace corresponds to processing one `Communications` document — from receiving the event or polling the API, through fetching the document, sending the email, and writing the status back.

A **span** is a single named, timed operation within a trace. Every trace is composed of one or more spans arranged in a parent-child tree. The root span represents the overall operation; child spans represent sub-operations.

For the email worker, a single trace might contain these spans:

```
process_communication (root span, duration: 312ms)
├── fetch_document       (child span, duration: 45ms)
├── serialize_body       (child span, duration: 3ms)
├── send_email           (child span, duration: 248ms)
└── update_status        (child span, duration: 16ms)
```

Each span records:
- A name
- A start time and duration
- A **trace ID** — shared by all spans in the same trace, used to correlate them
- A **span ID** — unique to this span
- A **parent span ID** — the span ID of the parent (absent on the root span)
- **Attributes** — key-value metadata (e.g. `doc_id`, `recipient_count`, `http.status_code`)
- **Events** — timestamped annotations within the span (e.g. "SMTP connection established")
- A **status** — OK or ERROR, with an optional error message

Traces are collected by a **tracer** in your code and exported to a backend. This lab uses **Jaeger**, which is already included in the MZinga `docker-compose.yml`. Jaeger provides a UI at `http://localhost:16686` where you can search traces by service name, time range, or trace ID, and visualise the span tree as a waterfall diagram.

**Why traces matter for this worker specifically:** when an email fails, a plain log tells you it failed. A trace tells you exactly which span failed, how long each preceding step took, and what the HTTP response code was from the MZinga API — all in one view, without correlating multiple log lines by hand.

---

## Prerequisites

### All platforms

- Lab 2 Part A completed and verified (`lab2-worker-rest/` working)
- The infrastructure running including Jaeger: `docker compose up database messagebus cache jaeger`
- Python 3.11+ with the Lab 2 venv active
- Access to Jaeger UI at `http://localhost:16686`
- Access to MZinga metrics at `http://localhost:3000/metrics` (already exposed by MZinga via `express-prom-bundle`)

### macOS / Linux / WSL

- No additional prerequisites

### Windows — containers running inside WSL

- All commands run from the WSL terminal
- Jaeger UI accessible at `http://localhost:16686` from the Windows browser

### Windows — containers running outside WSL

- Same as Lab 2 Windows prerequisites
- Jaeger ports (16686, 4318) must be accessible from the host — they are already mapped in `docker-compose.yml`

---

## Step 1 — Explore what MZinga already exposes

Before instrumenting the worker, understand the observability already present in MZinga. This gives you the baseline and shows what the worker's instrumentation should complement.

### 1.1 — Prometheus metrics from MZinga

Open `http://localhost:3000/metrics` in a browser. MZinga exposes Prometheus metrics via `express-prom-bundle` (configured in `server.ts`). You will see standard HTTP metrics labelled by method, path, status code, tenant, and version. These cover the MZinga side of every REST call the worker makes.

Identify the metric families:
- `http_request_duration_seconds` — histogram of HTTP request durations
- `http_requests_total` — counter of total requests by method, path, and status
- `up` — gauge indicating the service is running

### 1.2 — Traces from MZinga in Jaeger

Open `http://localhost:16686`. In the **Search** panel, select the MZinga service from the dropdown (it will appear after MZinga has handled at least one request). Search for recent traces and open one. Observe:

- The root span corresponds to an HTTP request to MZinga
- Child spans show MongoDB queries, Mongoose operations, and Express middleware
- Each span has attributes including the HTTP method, URL, status code, and MongoDB collection name

This is what MZinga's `tracing.ts` produces — auto-instrumentation via `getNodeAutoInstrumentations`. The worker's traces will appear as separate services in the same Jaeger instance.

### 1.3 — Read MZinga's tracing setup

Open `src/tracing.ts` in `mzinga-apps/`. Understand:
- How the `NodeTracerProvider` is configured with a `BatchSpanProcessor` and `OTLPTraceExporter`
- How `getNodeAutoInstrumentations` instruments Express, MongoDB, and HTTP automatically without any manual span creation
- How `DISABLE_TRACING=1` in `.env` disables the entire setup — this is why you set it in Lab 1

The Python worker will follow the same pattern: a tracer provider, an OTLP exporter pointing at Jaeger, and manual span creation around the key operations.

---

## Step 2 — Add structured logging to the worker

The worker currently uses Python's default `logging` with a plain text formatter. Replace this with structured JSON logging using the `structlog` library.

### 2.1 — Why structured logging

The current log format `%(asctime)s %(levelname)s %(message)s` produces lines like:

```
2025-01-15 10:23:01 INFO Processing communication abc123
```

This is readable but not queryable. With structured logging, the same event becomes:

```json
{"timestamp": "2025-01-15T10:23:01Z", "level": "info", "event": "processing_communication", "doc_id": "abc123", "service": "email-worker"}
```

Every field is addressable. A log aggregator can filter by `doc_id`, count events by `level`, or alert when `event == "email_failed"`.

### 2.2 — What to change in `worker.py`

Replace the `logging.basicConfig` setup with `structlog` configured to output JSON. Every log call should include the `doc_id` as a bound context variable so all log entries for a given communication are correlated by that field.

Add the following context fields to every log entry:
- `service` — a fixed string identifying this worker (e.g. `"email-worker"`)
- `doc_id` — bound when processing starts, cleared when processing ends
- `trace_id` and `span_id` — injected from the active OpenTelemetry span (added in Step 3)

Add `structlog` to `requirements.txt`.

---

## Step 3 — Add distributed tracing with OpenTelemetry

### 3.1 — Add the OpenTelemetry SDK

Add the following to `requirements.txt`:

- `opentelemetry-sdk` — core SDK (tracer provider, span processors)
- `opentelemetry-exporter-otlp-proto-http` — OTLP/HTTP exporter to send spans to Jaeger
- `opentelemetry-instrumentation-requests` — auto-instruments the `requests` library, creating spans for every HTTP call automatically

### 3.2 — Initialise the tracer provider

At startup, before any other code runs, initialise the OpenTelemetry SDK:

- Create a `Resource` with `service.name` set to `"email-worker"` and `service.version` set to the worker version
- Create an `OTLPSpanExporter` pointing at the Jaeger OTLP endpoint (`http://localhost:4318/v1/traces` by default — read from `OTEL_EXPORTER_OTLP_ENDPOINT` env var)
- Create a `BatchSpanProcessor` wrapping the exporter
- Create a `TracerProvider` with the resource and span processor, and register it as the global provider
- Call `RequestsInstrumentor().instrument()` to auto-instrument all `requests` calls

After this, every call to `requests.get`, `requests.post`, and `requests.patch` in the worker will automatically create a child span with the HTTP method, URL, and response status code — without any manual instrumentation.

### 3.3 — Add manual spans around key operations

Auto-instrumentation covers HTTP calls. The operations that are not HTTP calls — Slate serialisation and SMTP sending — need manual spans.

Obtain a tracer with `get_tracer("email-worker")`. Wrap the following operations in explicit spans:

- `process_communication` — root span for the entire processing of one document. Set `doc_id` as a span attribute. Set span status to ERROR if an exception is raised.
- `serialize_body` — child span wrapping the `slate_to_html` call. Set `node_count` as an attribute.
- `send_email` — child span wrapping the `smtplib` call. Set `recipient_count` as an attribute.

The HTTP calls to the MZinga API (`fetch_doc`, `update_status`, `login`) are already covered by `RequestsInstrumentor` — you do not need to wrap them manually.

### 3.4 — Inject trace context into structured logs

When a span is active, read the current `trace_id` and `span_id` from the OpenTelemetry context and add them to the structlog context. This links every log entry to the trace it belongs to, enabling you to jump from a log line to the corresponding trace in Jaeger.

Use `opentelemetry.trace.get_current_span()` to get the active span and extract its context.

---

## Step 4 — Add custom Prometheus metrics

### 4.1 — Add the OpenTelemetry metrics SDK

Add to `requirements.txt`:

- `opentelemetry-exporter-prometheus` — exposes metrics on an HTTP `/metrics` endpoint that Prometheus can scrape

### 4.2 — Initialise the metrics provider

At startup, alongside the tracer provider:

- Create a `PrometheusMetricReader` — this starts an HTTP server on a configurable port (default `8000`) that exposes the `/metrics` endpoint
- Create a `MeterProvider` with the reader and the same `Resource` used for tracing, and register it as the global provider
- Obtain a `Meter` with `get_meter("email-worker")`

### 4.3 — Define the metrics

Define the following instruments on the meter:

- `emails_processed_total` — an **UpDownCounter** (or Counter) that increments by 1 each time a communication is processed. Add attributes: `status` (`sent` or `failed`) and `recipient_count` (number of recipients).
- `email_processing_duration_seconds` — a **Histogram** that records the total duration of each `process_communication` span in seconds. This gives you percentile latency (p50, p95, p99) across all processed emails.
- `smtp_send_duration_seconds` — a **Histogram** that records only the SMTP send duration, isolating the external dependency latency from the internal processing time.
- `worker_poll_total` — a **Counter** that increments on each poll cycle, with attribute `result` (`found` or `empty`). This lets you see the poll rate and the ratio of productive to idle polls.

### 4.4 — Record measurements

Record measurements at the appropriate points in the worker:

- Increment `emails_processed_total` at the end of `process`, with `status="sent"` or `status="failed"`
- Record `email_processing_duration_seconds` at the end of `process` using the elapsed time of the root span
- Record `smtp_send_duration_seconds` inside the `send_email` span
- Increment `worker_poll_total` in the poll loop with `result="found"` or `result="empty"`

---

## Step 5 — Configure the environment

Add the following to the worker's `.env`:

- `OTEL_EXPORTER_OTLP_ENDPOINT` — the Jaeger OTLP endpoint (e.g. `http://localhost:4318`)
- `OTEL_SERVICE_NAME` — the service name as it will appear in Jaeger (e.g. `email-worker`)
- `PROMETHEUS_PORT` — the port for the `/metrics` endpoint (e.g. `8000`)

See the code snippets file for the full `.env` content.

---

## Step 6 — Verify logging

Start the worker and create a Communication document in the admin UI.

Confirm in the worker terminal:
- Log output is JSON, not plain text
- Each log entry for a processing cycle includes `doc_id`
- Log entries emitted while a span is active include `trace_id` and `span_id`
- The `trace_id` in the log matches the trace visible in Jaeger for the same processing cycle

---

## Step 7 — Verify traces in Jaeger

Open `http://localhost:16686`. Select the `email-worker` service from the dropdown.

Create a Communication document and wait for the worker to process it. Search for recent traces from `email-worker`.

Open the trace and verify:
- The root span `process_communication` is present with `doc_id` as an attribute
- Child spans `serialize_body` and `send_email` are present with their respective attributes
- HTTP spans for `GET /api/communications/:id` and `PATCH /api/communications/:id` are present as children, auto-created by `RequestsInstrumentor`
- The total trace duration matches the sum of child span durations
- If you trigger a failure (e.g. stop MailHog), the `send_email` span shows status ERROR with the exception message

Compare the span waterfall to the log output. The `trace_id` in the logs should match the trace ID in Jaeger.

---

## Step 8 — Verify metrics in Prometheus

Open `http://localhost:8000/metrics` in a browser. Confirm:
- `emails_processed_total` is present with `status` and `recipient_count` labels
- `email_processing_duration_seconds_bucket` is present (Prometheus histogram buckets)
- `smtp_send_duration_seconds_bucket` is present
- `worker_poll_total` is present with `result` label

Process several Communications documents and refresh the metrics endpoint. Confirm the counters increment and the histogram buckets accumulate.

To query the metrics in PromQL (if you have a Prometheus instance scraping the worker), try:

- `rate(emails_processed_total[5m])` — emails processed per second over the last 5 minutes
- `histogram_quantile(0.95, rate(email_processing_duration_seconds_bucket[5m]))` — 95th percentile processing time
- `rate(worker_poll_total{result="empty"}[5m]) / rate(worker_poll_total[5m])` — fraction of polls that found nothing to do

---

## Step 9 — Simulate and diagnose a failure

Stop MailHog (`docker stop <mailhog_container_id>`). Create a Communication document. Observe:

- The worker log shows a structured error entry with `doc_id`, `trace_id`, and the exception message
- In Jaeger, the trace for this document shows the `send_email` span with status ERROR
- The `emails_processed_total` counter increments with `status="failed"`
- The document status in the MZinga admin UI shows `failed`

Restart MailHog. Reset the document status to `pending` in the admin UI (or directly in MongoDB). Observe the worker picks it up and the next trace shows all spans green.

This is the observability loop: a failure surfaces in metrics (counter with `status=failed`), the trace shows exactly where it failed and why, and the structured log provides the full context including the `doc_id` to look up in the admin UI.

---

## What you have built

| Signal | Tool | What it answers |
|---|---|---|
| Structured logs | `structlog` + JSON | What happened, in what order, for which document |
| Traces | OpenTelemetry + Jaeger | Where time was spent, which operation failed, end-to-end latency |
| Metrics | OpenTelemetry + Prometheus | How many emails sent/failed, latency percentiles, poll efficiency |

| Instrument | Type | Labels |
|---|---|---|
| `emails_processed_total` | Counter | `status`, `recipient_count` |
| `email_processing_duration_seconds` | Histogram | — |
| `smtp_send_duration_seconds` | Histogram | — |
| `worker_poll_total` | Counter | `result` |

---

**Previous:** [07b — Lab 2 Code Snippets](07-lab2-code-snippets.md) · **Code snippets:** [08b — Lab 3 Code Snippets](08-lab3-code-snippets.md)
