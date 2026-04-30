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


# Load all variables from the local .env file
load_dotenv()

# Base URL of the local MZinga application
MZINGA_BASE_URL = os.getenv("MZINGA_BASE_URL", "http://localhost:3000").rstrip("/")

# Admin credentials used by the worker to log in to MZinga
MZINGA_EMAIL = os.getenv("MZINGA_EMAIL")
MZINGA_PASSWORD = os.getenv("MZINGA_PASSWORD")

# RabbitMQ address.
# RabbitMQ receives events from MZinga and gives them to the worker.
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672")

# RabbitMQ exchange used by MZinga.
# An exchange is like a message distributor.
EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "mzinga_events_durable")

# RabbitMQ queue used by this worker.
# A queue is like the worker mailbox.
QUEUE_NAME = os.getenv("QUEUE_NAME", "communications-email-worker")

# Routing key used to select only Communications events.
# This must match HOOKSURL_COMMUNICATIONS_AFTERCHANGE.
ROUTING_KEY = os.getenv("ROUTING_KEY", "HOOKSURL_COMMUNICATIONS_AFTERCHANGE")

# SMTP settings used to send emails
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")

# Reusable HTTP session for all REST API requests.
# After login, the JWT token is stored here.
session = requests.Session()


def login():
    """
    Log in to MZinga.

    MZinga returns a JWT token.
    The worker stores the token and uses it for the next API calls.
    """
    response = session.post(
        f"{MZINGA_BASE_URL}/api/users/login",
        json={
            "email": MZINGA_EMAIL,
            "password": MZINGA_PASSWORD,
        },
        timeout=15,
    )

    # Stop immediately if login failed
    response.raise_for_status()

    data = response.json()
    token = data["token"]

    # Save the token in the session.
    # From now on, every API request will include it automatically.
    session.headers.update({
        "Authorization": f"Bearer {token}"
    })

    print("Login successful")


def api_request(method, path, **kwargs):
    """
    Send one REST API request to MZinga.

    If the token is expired, the worker logs in again
    and retries the request one time.
    """
    url = f"{MZINGA_BASE_URL}{path}"

    # Send the request using the shared session
    response = session.request(method, url, timeout=15, **kwargs)

    # If the token is expired, log in again and retry
    if response.status_code == 401:
        print("Token expired or invalid. Logging in again...")
        login()
        response = session.request(method, url, timeout=15, **kwargs)

    # Stop if the request failed
    response.raise_for_status()
    return response


def fetch_communication(doc_id):
    """
    Get one Communication document from MZinga.

    RabbitMQ gives us only the document id.
    The full document is fetched from the REST API.

    depth=1 is important because it gives us the linked users
    with their email addresses.
    """
    response = api_request(
        "GET",
        f"/api/communications/{doc_id}?depth=1",
    )

    return response.json()


def update_communication_status(doc_id, status):
    """
    Update the status of one Communication document.

    Typical status flow:
    pending -> processing -> sent

    If something goes wrong:
    pending -> processing -> failed
    """
    api_request(
        "PATCH",
        f"/api/communications/{doc_id}",
        json={"status": status},
    )


def extract_emails(ref_list):
    """
    Extract email addresses from tos, ccs or bccs.

    With depth=1, each recipient contains a value object.
    The email is inside value.email.

    If something is empty or malformed, it is skipped.
    """
    emails = []

    # If there are no recipients, return an empty list
    if not ref_list:
        return emails

    # Sometimes the value may be one object instead of a list.
    # In that case, we turn it into a list.
    if isinstance(ref_list, dict):
        ref_list = [ref_list]

    # Read every recipient reference
    for ref in ref_list:
        if not isinstance(ref, dict):
            continue

        # The real user object is inside "value"
        value = ref.get("value", {})
        if not isinstance(value, dict):
            continue

        # Take the email if it exists
        email = value.get("email")
        if email:
            emails.append(email)

    return emails


def render_text_leaf(node):
    """
    Convert one text piece into safe HTML.

    It supports:
    - normal text
    - bold text
    - italic text
    """
    # Escape special HTML characters for safety
    text = escape(node.get("text", ""))

    # If the text is bold, wrap it in <strong>
    if node.get("bold"):
        text = f"<strong>{text}</strong>"

    # If the text is italic, wrap it in <em>
    if node.get("italic"):
        text = f"<em>{text}</em>"

    return text


def render_nodes(nodes):
    """
    Convert a list of Slate nodes into one HTML string.
    """
    # Render every node and join the results together
    return "".join(render_node(node) for node in (nodes or []))


def render_node(node):
    """
    Convert one Slate node into HTML.

    Supported types:
    - paragraph
    - h1
    - h2
    - ul
    - li
    - link

    Unknown types are not broken.
    Their children are still rendered.
    """
    # If this is a simple text node, render it directly
    if "text" in node:
        return render_text_leaf(node)

    # Render children first
    node_type = node.get("type")
    children_html = render_nodes(node.get("children", []))

    # Convert paragraph nodes
    if node_type == "paragraph":
        return f"<p>{children_html}</p>"

    # Convert heading level 1 nodes
    if node_type == "h1":
        return f"<h1>{children_html}</h1>"

    # Convert heading level 2 nodes
    if node_type == "h2":
        return f"<h2>{children_html}</h2>"

    # Convert unordered list nodes
    if node_type == "ul":
        return f"<ul>{children_html}</ul>"

    # Convert list item nodes
    if node_type == "li":
        return f"<li>{children_html}</li>"

    # Convert link nodes
    if node_type == "link":
        url = escape(node.get("url", "#"))
        return f'<a href="{url}">{children_html}</a>'

    # If the node type is unknown, return only its children
    return children_html


def send_email(subject, html_body, to_emails, cc_emails, bcc_emails):
    """
    Build and send one HTML email.

    To and Cc are visible in the email.
    Bcc is hidden, but still receives the email.
    """
    msg = MIMEMultipart("alternative")

    # Set the visible email headers
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_emails)
    msg["Cc"] = ", ".join(cc_emails)

    # Add the HTML body to the email
    msg.attach(MIMEText(html_body, "html"))

    # Real list of recipients used by SMTP
    all_recipients = to_emails + cc_emails + bcc_emails

    # Connect to the SMTP server and send the email
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())


def process_communication(doc):
    """
    Process one Communication document.

    The worker:
    1. checks the current status
    2. marks the document as processing
    3. extracts recipient emails
    4. converts the body to HTML
    5. sends the email
    6. marks the document as sent or failed
    """
    doc_id = doc["id"]

    print("Processing communication:", doc_id)

    # Read the current status of the document
    current_status = doc.get("status")

    # Do not process the same Communication twice
    if current_status in ["processing", "sent"]:
        print(f"Skipping communication {doc_id}, status is already {current_status}")
        return

    # Mark the Communication as being processed
    update_communication_status(doc_id, "processing")

    try:
        # Extract recipient email addresses
        to_emails = extract_emails(doc.get("tos", []))
        cc_emails = extract_emails(doc.get("ccs", []))
        bcc_emails = extract_emails(doc.get("bccs", []))

        # Convert the Slate body into HTML
        html_body = render_nodes(doc.get("body", []))

        # Read the email subject
        subject = doc.get("subject", "")

        # Print useful debug information
        print("Resolved TO emails:", to_emails)
        print("Resolved CC emails:", cc_emails)
        print("Resolved BCC emails:", bcc_emails)
        print("Generated HTML body:")
        print(html_body)

        # At least one recipient is required
        if not (to_emails or cc_emails or bcc_emails):
            raise Exception("No valid recipient email addresses found")

        # Send the email
        send_email(subject, html_body, to_emails, cc_emails, bcc_emails)

        # Mark the Communication as sent
        update_communication_status(doc_id, "sent")
        print("Email sent successfully")
        print("Status updated to sent")

    except Exception as e:
        # If something fails, mark the Communication as failed
        update_communication_status(doc_id, "failed")
        print("Email processing failed")
        print("Status updated to failed")
        print("Error:", e)


async def handle_message(message: aio_pika.IncomingMessage):
    """
    Handle one RabbitMQ message.

    RabbitMQ sends a message when a Communication changes.
    The worker reads the id, fetches the full document,
    and processes the email.
    """
    # Acknowledge the message only if this block finishes correctly
    async with message.process():
        # RabbitMQ messages are bytes, so convert them to text
        body = message.body.decode("utf-8")

        # Convert the JSON text into a Python dictionary
        event = json.loads(body)

        print("Received RabbitMQ event")

        # Read the important fields from the event
        data = event.get("data", {})
        operation = data.get("operation")
        doc = data.get("doc", {})
        doc_id = doc.get("id")

        print("Operation:", operation)
        print("Communication id:", doc_id)

        # Ignore update events.
        # The worker creates update events when it changes the status.
        if operation == "update":
            print("Skipping update event")
            return

        # Without an id, the worker cannot fetch the Communication
        if not doc_id:
            print("No document id found, skipping")
            return

        # Fetch the full Communication from MZinga
        full_doc = fetch_communication(doc_id)

        # Process the Communication
        process_communication(full_doc)


async def main():
    """
    Start the event-driven worker.

    The worker logs in to MZinga, connects to RabbitMQ,
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

    # Use the exchange where MZinga publishes events
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
        internal=True,
        auto_delete=False,
    )

    # Create the worker queue.
    # This queue stores messages for the worker.
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

    # Start listening for messages
    await queue.consume(handle_message)

    # Keep the worker running forever
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())