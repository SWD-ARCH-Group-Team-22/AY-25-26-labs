import os
import json
import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import aio_pika
import requests
from dotenv import load_dotenv


# Load variables from the .env file
load_dotenv()

# Address of MZinga
MZINGA_BASE_URL = os.getenv("MZINGA_BASE_URL", "http://localhost:3000").rstrip("/")

# Admin login used by the worker
MZINGA_EMAIL = os.getenv("MZINGA_EMAIL")
MZINGA_PASSWORD = os.getenv("MZINGA_PASSWORD")

# Address of RabbitMQ
# RabbitMQ receives events from MZinga
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672")

# RabbitMQ exchange: it distributes messages
EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "mzinga_events_durable")

# RabbitMQ queue: it is the worker's mailbox
QUEUE_NAME = os.getenv("QUEUE_NAME", "communications-email-worker")

# Event type that this worker wants to receive
ROUTING_KEY = os.getenv("ROUTING_KEY", "HOOKSURL_COMMUNICATIONS_AFTERCHANGE")

# SMTP settings used to send emails
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")

# HTTP session used for REST API requests
session = requests.Session()


def login():
    """
    Log in to MZinga.

    MZinga returns a token.
    The worker uses this token for the next REST API requests.
    """
    response = session.post(
        f"{MZINGA_BASE_URL}/api/users/login",
        json={
            "email": MZINGA_EMAIL,
            "password": MZINGA_PASSWORD,
        },
        timeout=15,
    )

    response.raise_for_status()

    token = response.json()["token"]

    session.headers.update({
        "Authorization": f"Bearer {token}"
    })

    print("Login successful")


def api_request(method, path, **kwargs):
    """
    Send one REST API request to MZinga.

    If the token is expired, the worker logs in again
    and tries the request one more time.
    """
    url = f"{MZINGA_BASE_URL}{path}"

    response = session.request(method, url, timeout=15, **kwargs)

    if response.status_code == 401:
        print("Token expired or invalid. Logging in again...")
        login()
        response = session.request(method, url, timeout=15, **kwargs)

    response.raise_for_status()
    return response


def fetch_communication(doc_id):
    """
    Get one Communication from MZinga.

    RabbitMQ gives us only the document id.
    So we use the REST API to get the full document.

    depth=1 gives us users with their email addresses.
    """
    response = api_request(
        "GET",
        f"/api/communications/{doc_id}?depth=1",
    )

    return response.json()


def update_communication_status(doc_id, status):
    """
    Change the status of a Communication.

    Example:
    pending -> processing -> sent
    """
    api_request(
        "PATCH",
        f"/api/communications/{doc_id}",
        json={"status": status},
    )


def extract_emails(ref_list):
    """
    Take email addresses from tos, ccs or bccs.

    If something is missing or empty, it skips it.
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
    Convert one text node into safe HTML.

    It also handles bold and italic text.
    """
    text = escape(node.get("text", ""))

    if node.get("bold"):
        text = f"<strong>{text}</strong>"

    if node.get("italic"):
        text = f"<em>{text}</em>"

    return text


def render_nodes(nodes):
    """
    Convert a list of Slate nodes into HTML.
    """
    return "".join(render_node(node) for node in (nodes or []))


def render_node(node):
    """
    Convert one Slate node into HTML.
    """
    if "text" in node:
        return render_text_leaf(node)

    node_type = node.get("type")
    children_html = render_nodes(node.get("children", []))

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

    return children_html


def send_email(subject, html_body, to_emails, cc_emails, bcc_emails):
    """
    Send one HTML email.
    """
    msg = MIMEMultipart("alternative")

    msg["Subject"] = subject or "(no subject)"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_emails)
    msg["Cc"] = ", ".join(cc_emails)

    msg.attach(MIMEText(html_body, "html"))

    all_recipients = to_emails + cc_emails + bcc_emails

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())


def process_communication(doc):
    """
    Send one Communication email.

    Steps:
    1. check the status
    2. set status to processing
    3. get recipients
    4. create the HTML body
    5. send the email
    6. set status to sent or failed
    """
    doc_id = doc["id"]

    print("Processing communication:", doc_id)

    current_status = doc.get("status")

    # Do not process the same Communication twice
    if current_status in ["processing", "sent"]:
        print(f"Skipping communication {doc_id}, status is already {current_status}")
        return

    # Mark the Communication as being processed
    update_communication_status(doc_id, "processing")

    try:
        # Get recipient email addresses
        to_emails = extract_emails(doc.get("tos", []))
        cc_emails = extract_emails(doc.get("ccs", []))
        bcc_emails = extract_emails(doc.get("bccs", []))

        # Convert the email body to HTML
        html_body = render_nodes(doc.get("body", []))
        subject = doc.get("subject", "")

        print("Resolved TO emails:", to_emails)
        print("Resolved CC emails:", cc_emails)
        print("Resolved BCC emails:", bcc_emails)
        print("Generated HTML body:")
        print(html_body)

        if not (to_emails or cc_emails or bcc_emails):
            raise Exception("No valid recipient email addresses found")

        # Send the email
        send_email(subject, html_body, to_emails, cc_emails, bcc_emails)

        # Mark the Communication as sent
        update_communication_status(doc_id, "sent")
        print("Email sent successfully")
        print("Status updated to sent")

    except Exception as e:
        # Mark the Communication as failed
        update_communication_status(doc_id, "failed")
        print("Email processing failed")
        print("Status updated to failed")
        print("Error:", e)


async def handle_message(message: aio_pika.IncomingMessage):
    """
    Run when RabbitMQ sends a message to the worker.

    The message says that a Communication changed.
    The worker reads the id, gets the full document from MZinga,
    and sends the email.
    """
    async with message.process():
        # RabbitMQ messages are bytes, so we convert them to text
        body = message.body.decode("utf-8")

        # Convert the JSON text into a Python dictionary
        event = json.loads(body)

        print("Received RabbitMQ event")

        # Get the useful data from the event
        data = event.get("data", {})
        operation = data.get("operation")
        doc = data.get("doc", {})
        doc_id = doc.get("id")

        print("Operation:", operation)
        print("Communication id:", doc_id)

        # Ignore update events.
        # They are created when the worker changes the status.
        if operation == "update":
            print("Skipping update event")
            return

        # If there is no id, the worker cannot fetch the Communication
        if not doc_id:
            print("No document id found, skipping")
            return

        # Get the full Communication from MZinga
        full_doc = fetch_communication(doc_id)

        # Send the email
        process_communication(full_doc)


async def main():
    """
    Start the worker.

    It logs in to MZinga, connects to RabbitMQ,
    creates its queue, and waits for messages.
    """
    print("Event-driven worker started")

    # Login to MZinga
    login()

    # Connect to RabbitMQ
    connection = await aio_pika.connect_robust(RABBITMQ_URL)

    # Open a RabbitMQ channel
    channel = await connection.channel()

    # Receive one message at a time
    await channel.set_qos(prefetch_count=1)

    # Use the exchange where MZinga sends events
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
        internal=True,
        auto_delete=False,
    )

    # Create the worker queue
    queue = await channel.declare_queue(
        QUEUE_NAME,
        durable=True,
    )

    # Connect the queue to the exchange.
    # Only messages with this routing key enter the queue.
    await queue.bind(exchange, routing_key=ROUTING_KEY)

    print("Listening for RabbitMQ events...")
    print("Exchange:", EXCHANGE_NAME)
    print("Queue:", QUEUE_NAME)
    print("Routing key:", ROUTING_KEY)

    # Start listening.
    # When a message arrives, handle_message runs.
    await queue.consume(handle_message)

    # Keep the worker running
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())