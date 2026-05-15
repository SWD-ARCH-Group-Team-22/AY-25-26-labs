import atexit
import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests
import structlog
from dotenv import load_dotenv
from prometheus_client import start_http_server

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

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

# Name used to identify this program in logs, traces and metrics
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "email-worker")

# Version of this worker
SERVICE_VERSION = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")

# Jaeger receives traces on this endpoint
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://localhost:4318",
).rstrip("/")

# Port used by this worker to expose Prometheus metrics
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "8000"))


def add_service_name(_, __, event_dict):
    """
    Add the name of this worker to every log.

    In a real system, logs can come from many programs.
    With this field we immediately know that the log comes from email-worker.
    """
    event_dict["service"] = SERVICE_NAME
    return event_dict


def configure_logging():
    """
    Configure structured logs.

    Instead of printing normal text, we print JSON logs.
    Each log contains info about: time, log level, worker name, and more.

    """
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            # Add contextual data to the log, if present
            structlog.contextvars.merge_contextvars,

            # Add "service": "email-worker" to every log
            add_service_name,

            # Add the log level, for example "info" or "error"
            structlog.processors.add_log_level,

            # Add the time of the log
            structlog.processors.TimeStamper(fmt="iso"),

            # Print the log as a JSON object
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_tracing():
    """
    Configure traces to be sent to Jaeger.

    A trace is like a timeline of what happens while one Communication is processed.
    """
    # Basic information used by Jaeger to recognize this worker
    resource = Resource.create({
        "service.name": SERVICE_NAME,
        "service.version": SERVICE_VERSION,
    })

    # Create the tracer provider that manages traces
    provider = TracerProvider(resource=resource)

    # Where to send the traces: http://localhost:4318/v1/traces 
    exporter = OTLPSpanExporter(
        endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces"
    )

    # We sent spans in groups, not one by one, to reduce overhead
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Make this tracer provider the one used by the whole program
    trace.set_tracer_provider(provider)

    # When the worker stops, send any traces still waiting
    atexit.register(provider.shutdown)

    # Automatically create spans for REST API calls made with requests
    RequestsInstrumentor().instrument()


def configure_metrics():
    """
    Configure metrics.

    Metrics are numbers that describe what the worker is doing:
    how many emails were processed, how many polls were empty,
    and how long the operations took.

    The worker exposes metrics at: http://localhost:8000/metrics
    """
    # Start the HTTP server used by Prometheus (http://localhost:8000/metrics)
    start_http_server(port=PROMETHEUS_PORT, addr="0.0.0.0")

    # This reader connects OpenTelemetry metrics to Prometheus
    reader = PrometheusMetricReader()

    # Basic information used by Jaeger to recognize this worker
    resource = Resource.create({
        "service.name": SERVICE_NAME,
        "service.version": SERVICE_VERSION,
    })
    
    # Create the meter provider that manage metrics
    provider = MeterProvider(
        resource=resource,
        metric_readers=[reader],
    )

    # Make this metrics provider the one used by the whole program
    metrics.set_meter_provider(provider)


# Activate JSON logging, tracing and metrics
configure_logging()
configure_tracing()
configure_metrics()

# Create a logger, tracer and meter 
logger = structlog.get_logger() 
tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

# Counts processed emails (both successful and failed)
emails_processed_total = meter.create_counter(
    name="emails_processed_total",
    description="Number of processed emails",
    unit="1",
)

#  Measures the total time needed to process one Communication
email_processing_duration_seconds = meter.create_histogram(
    name="email_processing_duration_seconds",
    description="Time spent processing one communication",
    unit="s",
)

# Measures only the time spent sending the email through SMTP
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
    all future requests automatically include: Authorization: Bearer <token>
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

    # 401 means the token is missing, expired or invalid
    if response.status_code == 401:
        logger.warning("token_expired_retrying_login")
        login()
        response = session.request(method, url, timeout=15, **kwargs)

    # Raise an exception
    response.raise_for_status()
    return response


def fetch_pending_communications():
    """
    Retrieve all Communication documents whose status is 'pending'.

    With depth=1, fields such as 'tos', 'ccs', and 'bccs' come back as full
    user objects, so the worker can read user emails directly from: value.email
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

    Example statuses used by the worker: 'processing', 'sent', 'failed'
    """
    api_request(
        "PATCH",
        f"/api/communications/{doc_id}",
        json={"status": status},
    )


def extract_emails(ref_list):
    """
    Extract email addresses from tos, ccs or bccs.

    Some entries may be empty or malformed, so we skip anything invalid.
    """
    emails = []

    if not ref_list:
        return emails

    # Sometimes the API may return one object instead of a list
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
    Convert one text node into safe HTML.

    This handles simple text, bold and italic.
    """
    # Escape special characters to avoid unsafe HTML
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
    Convert a list of Slate nodes into one HTML string.
    """
    return "".join(render_node(node) for node in (nodes or []))


def render_node(node):
    """
    Render one Slate node into HTML.

    If the node type is unknown, we still render its children.
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
    
    Workflow:
    1. Create the email headers, including Subject, From, To and Cc
    2. Attach the HTML body to the email
    3. add all recipient emails, including Bcc, to the SMTP envelope
    4. Open a connection to the SMTP server and send the email
    """
    msg = MIMEMultipart("alternative")

    # Step 1: Set visible email headers
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_emails)
    msg["Cc"] = ", ".join(cc_emails)

    # Step 2: Attach the HTML body to the email
    msg.attach(MIMEText(html_body, "html"))

    # Step 3: Add all recipient emails, including Bcc, to the SMTP envelope
    all_recipients = to_emails + cc_emails + bcc_emails

    # Step 4: Open an SMTP connection and send the email
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
    1. Mark the document as 'processing'
    2. Extract recipient emails from tos/ccs/bccs
    3. Convert the body from Slate to HTML format
    4. Send the email
    5. Mark the document as 'sent' on success
    6. Mark the document as 'failed' if any exception occurs
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
    1. Authenticates against the REST API
    2. Polls for pending Communication documents
    3. Processes each pending document
    4. Sleeps for a few seconds
    5. Repeats forever
    """
    logger.info("worker_started")

    # Step 1: authenticate against the REST API to get a valid token
    login()

    # Step 2-5: repeat forever
    while True:
        try:
            # Step 2: fetch all pending communications
            pending_docs = fetch_pending_communications()

            # If no pending documents exist
            if not pending_docs:
                
                # Write a log to indicate that the poll found no pending documents
                logger.info("poll_empty")
                
                # Record a metric to count how many times the worker polled with no pending documents
                worker_poll_total.add(1, {"result": "empty"})
                
                # Wait a few seconds before the next poll 
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # If found, log how many pending documents were found
            logger.info("poll_found", count=len(pending_docs))
            worker_poll_total.add(1, {"result": "found"})

            # Step 3: Process each pending communication one by one
            for doc in pending_docs:
                with tracer.start_as_current_span("worker_cycle"):
                    process_communication(doc)

        # If any unexpected exception occurs, log it and continue with the next cycle
        except Exception as e:
            logger.error("worker_loop_failed", error=str(e))

        # Step 4: Wait few seconds before the next polling cycle
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main() 