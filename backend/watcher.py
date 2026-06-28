import imaplib, email, json, os
from email.header import decode_header
import anthropic
import firebase_admin
from firebase_admin import credentials, firestore


def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return ""
    charset = msg.get_content_charset() or "utf-8"
    return msg.get_payload(decode=True).decode(charset, errors="replace")


def decode_field(value):
    if value is None:
        return ""
    out = ""
    for text, charset in decode_header(value):
        out += text.decode(charset or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


def ai_extract(subject, body, sender, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""Extract repair-order details from this aviation MRO email. Return ONLY a JSON object with keys: customer, rfq, ro, pn, sn, stage, summary.
For stage pick: RFQ/capability request, Part shipped, Quote issued, Quote approval, Repair complete, or Unclassified.
If a field is missing use null.
FROM: {sender}
SUBJECT: {subject}
BODY: {body}"""
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    try:
        return json.loads(raw)
    except:
        return {"stage":"Unclassified","customer":None,"ro":None,"pn":None,"sn":None,"rfq":None,"summary":None}


def main():
    import traceback
    try:
        EMAIL    = os.environ["MRO_EMAIL"]
        PASSWORD = os.environ["MRO_APP_PASSWORD"]
        ANT_KEY  = os.environ["ANTHROPIC_KEY"]
        SA_RAW   = os.environ["FIREBASE_SERVICE_ACCOUNT"]

        print(f"Service account JSON length: {len(SA_RAW)}")
        SA_JSON = json.loads(SA_RAW)
        print(f"Project ID: {SA_JSON.get('project_id')}")

        cred = credentials.Certificate(SA_JSON)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firestore connected")

        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(EMAIL, PASSWORD)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        email_ids = data[0].split()
        print(f"Found {len(email_ids)} new emails")

        for num in email_ids:
            typ, msg_data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_field(msg["Subject"])
            body    = get_body(msg)
            sender  = decode_field(msg["From"])
            fields  = ai_extract(subject, body, sender, ANT_KEY)
            fields["date"]     = msg["Date"] or ""
            fields["from"]     = sender
            fields["subject"]  = subject
            fields["body"]     = body[:500]
            fields["messages"] = [{"dir":"inbound","from":sender,"time":msg["Date"] or "","body":body[:500]}]
            fields["docs"]     = []
            print(f"Writing to Firestore: {fields.get('customer')} | {fields.get('stage')}")
            ref = db.collection("emails").add(fields)
            print(f"Saved with ID: {ref[1].id}")

        M.logout()
        print("Done")

    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
