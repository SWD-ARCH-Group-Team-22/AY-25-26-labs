import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests
from dotenv import load_dotenv


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

    print("Login successful")


def api_request(method, path, **kwargs):
    """
    Execute one REST API request using the shared HTTP session.

    If the server returns HTTP 401, the token is considered expired or invalid.
    In that case, the worker performs login again and retries the request once.
    """
    url = f"{MZINGA_BASE_URL}{path}"

    response = session.request(method, url, timeout=15, **kwargs)

    if response.status_code == 401:
        print("Token expired or invalid. Logging in again...")
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
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())


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

    print("Processing communication:", doc_id)

    # Step 1: claim the document by moving it from pending to processing
    update_communication_status(doc_id, "processing")

    try:
        # Step 2: extract recipient email addresses from resolved relationship fields
        to_emails = extract_emails(doc.get("tos", []))
        cc_emails = extract_emails(doc.get("ccs", []))
        bcc_emails = extract_emails(doc.get("bccs", []))

        # Step 3: convert the Slate AST body into HTML
        html_body = render_nodes(doc.get("body", []))
        subject = doc.get("subject", "")

        # Debug output to help during development and testing
        print("Resolved TO emails:", to_emails)
        print("Resolved CC emails:", cc_emails)
        print("Resolved BCC emails:", bcc_emails)
        print("Generated HTML body:")
        print(html_body)

        # At least one valid recipient must exist
        if not (to_emails or cc_emails or bcc_emails):
            raise Exception("No valid recipient email addresses found")

        # Step 4: send the email
        send_email(subject, html_body, to_emails, cc_emails, bcc_emails)

        # Step 5: mark the communication as successfully sent
        update_communication_status(doc_id, "sent")
        print("Email sent successfully")
        print("Status updated to sent")

    except Exception as e:
        # Step 6: mark the communication as failed if anything goes wrong
        update_communication_status(doc_id, "failed")
        print("Email processing failed")
        print("Status updated to failed")
        print("Error:", e)


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
    print("Worker started. Polling REST API for pending communications...")

    # Perform the initial login before entering the loop
    login()

    while True:
        try:
            # Fetch all pending communications
            pending_docs = fetch_pending_communications()

            # If no pending documents exist, wait and try again later
            if not pending_docs:
                print("No pending communication found. Sleeping...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            print(f"Found {len(pending_docs)} pending communication(s).")

            # Process each pending communication one by one
            for doc in pending_docs:
                process_communication(doc)

        except Exception as e:
            # Catch unexpected errors in the main loop so the worker keeps running
            print("Worker loop failed")
            print("Error:", e)

        # Wait before the next polling cycle
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main() 