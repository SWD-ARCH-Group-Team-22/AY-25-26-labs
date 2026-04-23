import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


# Load environment variables from the .env file
load_dotenv()

# Read MongoDB connection string from environment
MONGODB_URI = os.getenv("MONGODB_URI")

# Read polling interval from environment (default: 5 seconds)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))

# Read SMTP configuration from environment
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")

# Create MongoDB client using the URI
client = MongoClient(MONGODB_URI)

# Get the default database specified in the URI ("mzinga")
db = client["mzinga"]

# Get references to the collections used by the worker
communications = db["communications"]
users = db["users"]


def claim_pending_communication():
    """
    Find one communication document with status = "pending"
    and immediately update its status in MongoDB to "processing".

    This acts as a claim step, so the worker takes ownership
    of the document before doing any further work.
    """
    claimed_doc = communications.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing"}},
    )

    return claimed_doc


def resolve_user_email(ref):
    """
    Resolve one Payload relationship reference into a real email address.

    Expected ref format:
    {
        "relationTo": "users",
        "value": <ObjectId or string>
    }
    """
    # The reference must exist and must point to the "users" collection
    if not ref or ref.get("relationTo") != "users":
        return None

    # Extract the referenced user id
    user_id = ref.get("value")

    # If no id is present, the user cannot be resolved
    if not user_id:
        return None

    # Convert string ids to ObjectId, if needed
    if isinstance(user_id, str):
        try:
            user_id = ObjectId(user_id)
        except Exception:
            return None

    # Look up the user document in MongoDB
    user = users.find_one({"_id": user_id})

    # If the user does not exist, return None
    if not user:
        return None

    # Return the user's email address
    return user.get("email")


def resolve_recipients(ref_list):
    """
    Resolve a list of Payload relationship references
    into a list of real email addresses.
    """
    emails = []

    for ref in ref_list or []:
        email = resolve_user_email(ref)
        if email:
            emails.append(email)

    return emails


def render_text_leaf(node):
    """
    Render a text leaf node from Slate AST into HTML text.

    Supports:
    - plain text
    - bold
    - italic
    """
    # Escape special HTML characters for safety
    text = escape(node.get("text", ""))

    # Apply bold formatting if present
    if node.get("bold"):
        text = f"<strong>{text}</strong>"

    # Apply italic formatting if present
    if node.get("italic"):
        text = f"<em>{text}</em>"

    return text


def render_nodes(nodes):
    """
    Render a list of Slate nodes into a single HTML string.
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
    - text leaf nodes
    """
    # Text leaf node
    if "text" in node:
        return render_text_leaf(node)

    # Render children first (recursive step)
    node_type = node.get("type")
    children_html = render_nodes(node.get("children", []))

    # Map Slate node types to HTML tags
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

    # Fallback: if the node type is unknown,
    # return only the rendered children
    return children_html


def send_email(subject, html_body, to_emails, cc_emails, bcc_emails):
    """
    Build and send an HTML email using the configured SMTP server.
    """
    # Create a multipart message that can contain HTML content
    msg = MIMEMultipart("alternative")

    # Set visible email headers
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(to_emails)
    msg["Cc"] = ", ".join(cc_emails)

    # Attach the HTML body
    msg.attach(MIMEText(html_body, "html"))

    # Build the full recipient list for actual delivery
    # BCC recipients are included here, but not in visible headers
    all_recipients = to_emails + cc_emails + bcc_emails

    # Send the email through SMTP
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())


def main():
    """
    Main worker loop:
    - poll MongoDB for pending communications
    - claim one document
    - resolve recipients
    - render HTML body
    - send the email
    - update status to sent or failed
    """
    print("Worker started. Polling for pending communications...")

    while True:
        # Try to claim one pending communication
        claimed_doc = claim_pending_communication()

        if not claimed_doc:
            # No pending document found: wait and try again later
            print("No pending communication found. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Debug: show which document was claimed
        print("Claimed communication:", claimed_doc["_id"])

        try:
            # Resolve recipient email addresses from Payload references
            to_emails = resolve_recipients(claimed_doc.get("tos", []))
            cc_emails = resolve_recipients(claimed_doc.get("ccs", []))
            bcc_emails = resolve_recipients(claimed_doc.get("bccs", []))

            # Convert Slate AST body to HTML
            html_body = render_nodes(claimed_doc.get("body", []))

            # Read subject from the communication document
            subject = claimed_doc.get("subject", "")

            # Debug output
            print("Resolved TO emails:", to_emails)
            print("Resolved CC emails:", cc_emails)
            print("Resolved BCC emails:", bcc_emails)
            print("Generated HTML body:")
            print(html_body)

            # At least one recipient must exist
            if not (to_emails or cc_emails or bcc_emails):
                raise Exception("No valid recipient email addresses found")

            # Send the email
            send_email(subject, html_body, to_emails, cc_emails, bcc_emails)

            # Mark the communication as sent
            communications.update_one(
                {"_id": claimed_doc["_id"]},
                {"$set": {"status": "sent"}},
            )

            print("Email sent successfully")
            print("Status updated to sent")

        except Exception as e:
            # Mark the communication as failed if anything goes wrong
            communications.update_one(
                {"_id": claimed_doc["_id"]},
                {"$set": {"status": "failed"}},
            )

            # Log the error for debugging
            print("Email processing failed")
            print("Status updated to failed")
            print("Error:", e)


if __name__ == "__main__":
    main()