import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
import logging
import structlog
import atexit

import requests
from dotenv import load_dotenv

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from prometheus_client import start_http_server
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader

# Load all variables from the local .env file
load_dotenv()

# Base URL of the local MZinga application
MZINGA_BASE_URL = os.getenv("MZINGA_BASE_URL", "http://localhost:3000").rstrip("/")

# Admin credentials used by the worker to authenticate against the REST API
MZINGA_EMAIL = os.getenv("MZINGA_EMAIL")
MZINGA_PASSWORD = os.getenv("MZINGA_PASSWORD")

# Time to wait between polling attempts when no pending documents are found
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))

# SMTP settings used to send emails
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")

# Name of this worker.
# It will appear in every structured log.
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "email-worker")

# Worker version shown in Jaeger
SERVICE_VERSION = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")

# Jaeger endpoint used to receive traces
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://localhost:4318",
).rstrip("/")

# Port where the worker exposes its metrics
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "8000"))

def add_service_name(_, __, event_dict):
    """
    Add the worker name to every log.
    """
    event_dict["service"] = SERVICE_NAME
    return event_dict


def configure_logging():
    """
    Configure logs as JSON objects.

    Each log will have clear fields like:
    event, level, timestamp and service.
    """
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_service_name,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_tracing():
    """
    Configure OpenTelemetry tracing.

    Traces are sent to Jaeger.
    Each trace shows what the worker did and how long each step took.
    """
    resource = Resource.create({
        "service.name": SERVICE_NAME,
        "service.version": SERVICE_VERSION,
    })

    provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(
        endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces"
    )

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)

    # Automatically create spans for all requests calls
    RequestsInstrumentor().instrument()


def configure_metrics():
    """
    Configure Prometheus metrics.

    The worker exposes metrics at:
    http://localhost:8000/metrics
    """
    # Start the HTTP server used by Prometheus
    start_http_server(port=PROMETHEUS_PORT, addr="0.0.0.0")

    # This reader connects OpenTelemetry metrics to Prometheus
    reader = PrometheusMetricReader()

    resource = Resource.create({
        "service.name": SERVICE_NAME,
        "service.version": SERVICE_VERSION,
    })

    provider = MeterProvider(
        resource=resource,
        metric_readers=[reader],
    )

    metrics.set_meter_provider(provider)


# Activate JSON logging
configure_logging()

# Activate tracing
configure_tracing()

# Activate metrics
configure_metrics()

# Logger used by the worker
logger = structlog.get_logger()

# Tracer used to create spans manually
tracer = trace.get_tracer(SERVICE_NAME)

# Meter used to create metrics
meter = metrics.get_meter(SERVICE_NAME)

# Counts processed emails
emails_processed_total = meter.create_counter(
    name="emails_processed_total",
    description="Number of processed emails",
    unit="1",
)

# Measures total processing time
email_processing_duration_seconds = meter.create_histogram(
    name="email_processing_duration_seconds",
    description="Time spent processing one communication",
    unit="s",
)

# Measures SMTP sending time
smtp_send_duration_seconds = meter.create_histogram(
    name="smtp_send_duration_seconds",
    description="Time spent sending the email through SMTP",
    unit="s",
)

# Counts polling cycles
worker_poll_total = meter.create_counter(
    name="worker_poll_total",
    description="Number of polling cycles",
    unit="1",
)

# Reusable HTTP session for all REST API requests
# This lets us keep the Authorization header once login succeeds
session = requests.Session()

def login():
    """
    Authenticate against the MZinga REST API.

    The login endpoint returns a JWT token.
    Once retrieved, the token is stored inside the session headers so that
    all future requests automatically include:

        Authorization: Bearer <token>
    """
    response = session.post(
        f"{MZINGA_BASE_URL}/api/users/login",
        json={
            "email": MZINGA_EMAIL,
            "password": MZINGA_PASSWORD,
        },
        timeout=15,
    )

    # Raise an exception immediately if login failed
    response.raise_for_status()

    data = response.json()
    token = data["token"]

    # Save the token in the session so we do not have to pass it manually
    session.headers.update({
        "Authorization": f"Bearer {token}"
    })

    logger.info("login_successful")


def api_request(method, path, **kwargs):
    """
    Execute one REST API request using the shared HTTP session.

    If the server returns HTTP 401, the token is considered expired or invalid.
    In that case, the worker performs login again and retries the request once.
    """
    url = f"{MZINGA_BASE_URL}{path}"

    response = session.request(method, url, timeout=15, **kwargs)

    if response.status_code == 401:
        logger.warning("token_expired_retrying_login")
        login()
        response = session.request(method, url, timeout=15, **kwargs)

    # Raise an exception if the request still failed
    response.raise_for_status()
    return response


def fetch_pending_communications():
    """
    Retrieve all Communication documents whose status is 'pending'.

    The query uses:
    - where[status][equals]=pending  -> only pending communications
    - depth=1                        -> resolve relationship fields one level deep

    With depth=1, fields such as 'tos', 'ccs', and 'bccs' come back as full
    user objects, so the worker can read user emails directly from:

        value.email
    """
    response = api_request(
        "GET",
        "/api/communications?where[status][equals]=pending&depth=1",
    )

    data = response.json()
    return data.get("docs", [])


def update_communication_status(doc_id, status):
    """
    Update the status of one Communication document using a PATCH request.

    Example statuses used by the worker:
    - processing
    - sent
    - failed
    """
    api_request(
        "PATCH",
        f"/api/communications/{doc_id}",
        json={"status": status},
    )


def extract_emails(ref_list):
    """
    Extract email addresses from REST API relationship objects.

    Expected structure with depth=1:
    {
        "relationTo": "users",
        "value": {
            "id": "...",
            "email": "user@example.com"
        }
    }

    This function is slightly defensive:
    - if the input is empty, it returns an empty list
    - if the input is a single dict instead of a list, it converts it to a list
    - if an entry has no valid value/email, it skips it
    """
    emails = []

    if not ref_list:
        return emails

    if isinstance(ref_list, dict):
        ref_list = [ref_list]

    for ref in ref_list:
        if not isinstance(ref, dict):
            continue

        value = ref.get("value", {})
        if not isinstance(value, dict):
            continue

        email = value.get("email")
        if email:
            emails.append(email)

    return emails


def render_text_leaf(node):
    """
    Render one Slate text leaf into safe HTML.

    Supported inline styles:
    - plain text
    - bold
    - italic
    """
    # Escape HTML-sensitive characters for safety
    text = escape(node.get("text", ""))

    # Apply bold formatting
    if node.get("bold"):
        text = f"<strong>{text}</strong>"

    # Apply italic formatting
    if node.get("italic"):
        text = f"<em>{text}</em>"

    return text


def render_nodes(nodes):
    """
    Render a list of Slate nodes into one HTML string.
    """
    return "".join(render_node(node) for node in (nodes or []))


def render_node(node):
    """
    Render one Slate node into HTML.

    Supported node types:
    - paragraph
    - h1
    - h2
    - ul
    - li
    - link
    - text nodes

    If a node type is unknown, the function falls back to returning
    only the rendered children.
    """
    # Text leaf node
    if "text" in node:
        return render_text_leaf(node)

    # Render child nodes first (recursive step)
    node_type = node.get("type")
    children_html = render_nodes(node.get("children", []))

    # Map known Slate node types to HTML tags
    if node_type == "paragraph":
        return f"<p>{children_html}</p>"
    if node_type == "h1":
        return f"<h1>{children_html}</h1>"
    if node_type == "h2":
        return f"<h2>{children_html}</h2>"
    if node_type == "ul":
        return f"<ul>{children_html}</ul>"
    if node_type == "li":
        return f"<li>{children_html}</li>"
    if node_type == "link":
        url = escape(node.get("url", "#"))
        return f'<a href="{url}">{children_html}</a>'

    # Fallback for unsupported or unknown node types
    return children_html


def send_email(subject, html_body, to_emails, cc_emails, bcc_emails):
    """
    Build and send one HTML email through the configured SMTP server.

    - 'To' and 'Cc' are visible in the email headers
    - 'Bcc' recipients are only included in the SMTP recipient list
    """
    msg = MIMEMultipart("alternative")

    # Visible email headers
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_emails)
    msg["Cc"] = ", ".join(cc_emails)

    # Attach the HTML version of the email body
    msg.attach(MIMEText(html_body, "html"))

    # Actual recipient list used by SMTP delivery
    all_recipients = to_emails + cc_emails + bcc_emails

    # Open an SMTP connection and send the email
    # Open an SMTP connection and send the email
    with tracer.start_as_current_span("send_email") as span:
        span.set_attribute("recipient_count", len(all_recipients))
        span.set_attribute("smtp.host", SMTP_HOST)
        span.set_attribute("smtp.port", SMTP_PORT)

        smtp_start_time = time.perf_counter()

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())

        finally:
            smtp_duration = time.perf_counter() - smtp_start_time
            smtp_send_duration_seconds.record(smtp_duration)


def process_communication(doc):
    """
    Process one Communication document.

    Workflow:
    1. mark the document as 'processing'
    2. extract recipient emails from tos/ccs/bccs
    3. convert the Slate body to HTML
    4. send the email
    5. mark the document as 'sent' on success
    6. mark the document as 'failed' if any exception occurs
    """
    doc_id = doc["id"]
    processing_start_time = time.perf_counter()
    recipient_count = 0

    with tracer.start_as_current_span("process_communication") as span:
        # Add useful information to the trace
        span.set_attribute("doc_id", doc_id)

        logger.info("processing_communication", doc_id=doc_id)

        # Step 1: claim the document by moving it from pending to processing
        update_communication_status(doc_id, "processing")

        try:
            # Step 2: extract recipient email addresses from resolved relationship fields
            to_emails = extract_emails(doc.get("tos", []))
            cc_emails = extract_emails(doc.get("ccs", []))
            bcc_emails = extract_emails(doc.get("bccs", []))

            recipient_count = len(to_emails) + len(cc_emails) + len(bcc_emails)
            span.set_attribute("recipient_count", recipient_count)


            # Step 3: convert the Slate AST body into HTML
            with tracer.start_as_current_span("serialize_body") as serialize_span:
                body_nodes = doc.get("body", [])
                serialize_span.set_attribute("node_count", len(body_nodes))
                html_body = render_nodes(body_nodes)

            subject = doc.get("subject", "")

            # Log how many recipients were found
            logger.info(
                "recipients_resolved",
                doc_id=doc_id,
                to_count=len(to_emails),
                cc_count=len(cc_emails),
                bcc_count=len(bcc_emails),
            )

            # At least one valid recipient must exist
            if not (to_emails or cc_emails or bcc_emails):
                raise Exception("No valid recipient email addresses found")

            # Step 4: send the email
            send_email(subject, html_body, to_emails, cc_emails, bcc_emails)

            # Step 5: mark the communication as successfully sent
            update_communication_status(doc_id, "sent")
            logger.info("email_sent", doc_id=doc_id)
            logger.info("status_updated", doc_id=doc_id, status="sent")

            span.set_status(Status(StatusCode.OK))

            # Record metrics for a successful email
            processing_duration = time.perf_counter() - processing_start_time
            email_processing_duration_seconds.record(processing_duration)
            emails_processed_total.add(
                1,
                {
                    "status": "sent",
                    "recipient_count": recipient_count,
                },
            )
        except Exception as e:
            # Mark the trace as failed
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))

            # Step 6: mark the communication as failed if anything goes wrong
            update_communication_status(doc_id, "failed")
            logger.error("email_failed", doc_id=doc_id, error=str(e))
            logger.info("status_updated", doc_id=doc_id, status="failed")
            # Record metrics for a failed email
            processing_duration = time.perf_counter() - processing_start_time
            email_processing_duration_seconds.record(processing_duration)
            emails_processed_total.add(
                1,
                {
                    "status": "failed",
                    "recipient_count": recipient_count,
                },
            )

def main():
    """
    Main worker loop.

    The worker:
    1. authenticates against the REST API
    2. polls for pending Communication documents
    3. processes each pending document
    4. sleeps for a few seconds
    5. repeats forever
    """
    logger.info("worker_started")

    # Perform the initial login before entering the loop
    login()

    while True:
        try:
            # Fetch all pending communications
            pending_docs = fetch_pending_communications()

            # If no pending documents exist, wait and try again later
            if not pending_docs:
                logger.info("poll_empty")
                worker_poll_total.add(1, {"result": "empty"})
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            logger.info("poll_found", count=len(pending_docs))
            worker_poll_total.add(1, {"result": "found"})

            # Process each pending communication one by one
            for doc in pending_docs:
                with tracer.start_as_current_span("worker_cycle"):
                    process_communication(doc)

        except Exception as e:
            # Catch unexpected errors in the main loop so the worker keeps running
            logger.error("worker_loop_failed", error=str(e))

        # Wait before the next polling cycle
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main() 