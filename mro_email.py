import imaplib
import email
import time
import json
import subprocess
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


def extract_time(date_str):
    """Extract HH:MM from an email date string like 'Sun, 28 Jun 2026 14:49:21 -0500'."""
    if not date_str:
        return "—"
    for part in date_str.split():
        if len(part) == 8 and part[2] == ':' and part[5] == ':':
            return part[:5]
    return "—"


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


def save_data_js():
    """Write data.js so the webpage can load real email data."""
    if not parsed_emails:
        return
    entries = []
    for e in parsed_emails:
        entries.append({
            "customer": e.get("customer") or "Unknown",
            "rfq": e.get("rfq"),
            "ro": e.get("ro") or "—",
            "pn": e.get("pn"),
            "sn": e.get("sn"),
            "stage": e.get("stage") or "Unclassified",
            "summary": e.get("summary") or "",
            "date": e.get("date") or "",
            "from": e.get("from_addr") or "",
            "body": e.get("body_text") or "",
            "messages": [
                {
                    "dir": "inbound",
                    "from": e.get("from_addr") or "",
                    "time": extract_time(e.get("date") or ""),
                    "body": e.get("body_text") or ""
                }
            ],
            "docs": []
        })
    js_content = "var MRO_DATA = " + json.dumps(entries, indent=2, ensure_ascii=False) + ";\n"
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js_content)
    print("Saved to data.js")


def git_push():
    """Commit updated data files and push to GitHub so the live page reflects new emails."""
    try:
        subprocess.run(["git", "add", "data.js", "parsed_emails.csv"], check=True)
        subprocess.run(["git", "commit", "-m", "Update email data"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("Pushed to GitHub")
    except subprocess.CalledProcessError as e:
        print("Git push failed:", e)


print("Watching inbox... (press Ctrl+C to stop)")
try:
    while True:
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            M.login("example.mro.email@gmail.com", APP_PASSWORD)
            M.select("INBOX")

            typ, data = M.search(None, "UNSEEN")
            new_count = 0
            for num in data[0].split():
                typ, msg_data = M.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = decode_field(msg["Subject"])
                body    = get_body(msg)
                sender  = decode_field(msg["From"])

                fields = ai_extract(subject, body, sender)
                fields["date"]      = msg["Date"] or ""
                fields["from_addr"] = sender
                fields["body_text"] = body[:1000]
                parsed_emails.append(fields)
                new_count += 1

                print(f"NEW [{fields.get('stage')}] from {fields.get('customer')}")
                print(f"     RO={fields.get('ro')}  PN={fields.get('pn')}  SN={fields.get('sn')}")
                print(f"     {fields.get('summary')}")

            M.logout()
            if new_count > 0:
                save_csv()
                save_data_js()
                git_push()
        except Exception as e:
            print("Error this round:", e)

        time.sleep(30)
except KeyboardInterrupt:
    print("\nStopped.")
    save_csv()
    save_data_js()
    git_push()
