import imaplib
import email
import time
import json
from email.header import decode_header
from getpass import getpass
import anthropic
import pandas as pd

def get_body(msg):
    """Pull the plain-text body out of an email, handling multipart messages."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")


def decode_field(value):
    """Decode an email header (subjects/names can be encoded)."""
    if value is None:
        return ""
    out = ""
    for text, charset in decode_header(value):
        out += text.decode(charset or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


ANTHROPIC_KEY = getpass("Anthropic API key: ")
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def ai_extract(subject, body, sender):
    """Use Claude to pull structured repair-order fields from an email."""
    prompt = f"""Extract repair-order details from this aviation MRO email. Return ONLY a JSON object, no other text, with these keys: customer, rfq, ro, pn (part number), sn (serial number), stage, summary.

For "stage", pick the best fit: "RFQ/capability request", "Part shipped", "Quote issued", "Quote approval", "Repair complete", or "Unclassified".
For "summary", write one short sentence on what the email is asking or saying.
If a field isn't present, use null.

FROM: {sender}
SUBJECT: {subject}
BODY: {body}"""

    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "could not parse", "raw": raw}

APP_PASSWORD = getpass("App password: ")
parsed_emails = []

def save_csv():
    if parsed_emails:
        pd.DataFrame(parsed_emails).to_csv("parsed_emails.csv", index=False)
        print("Saved to parsed_emails.csv")

print("Watching inbox... (press Ctrl+C to stop)")
try:
    while True:
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            M.login("example.mro.email@gmail.com", APP_PASSWORD)
            M.select("INBOX")

            typ, data = M.search(None, "UNSEEN")
            for num in data[0].split():
                typ, msg_data = M.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = decode_field(msg["Subject"])
                body    = get_body(msg)
                sender  = decode_field(msg["From"])

                fields = ai_extract(subject, body, sender)
                fields["date"] = msg["Date"] or ""
                parsed_emails.append(fields)

                print(f"NEW [{fields.get('stage')}] from {fields.get('customer')}")
                print(f"     RO={fields.get('ro')}  PN={fields.get('pn')}  SN={fields.get('sn')}")
                print(f"     {fields.get('summary')}")

            M.logout()
            save_csv()
        except Exception as e:
            print("Error this round:", e)

        time.sleep(30)
except KeyboardInterrupt:
    print("\nStopped.")
    save_csv()