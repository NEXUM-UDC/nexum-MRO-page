import imaplib, email, json, os, traceback
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


def get_attachments(msg):
    """Detect attachments and return list of names."""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            filename = part.get_filename()
            if filename and content_type != "text/plain" and content_type != "text/html":
                attachments.append({"name": filename, "tag": "ATTACHED"})
    return attachments


def decode_field(value):
    if value is None:
        return ""
    out = ""
    for text, charset in decode_header(value):
        out += text.decode(charset or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


def normalize_ro(ro):
    if not ro:
        return None
    return str(ro).upper().replace("RO-","").replace("RO","").strip()


def ai_extract(subject, body, sender, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""Extract repair-order details from this aviation MRO email. Return ONLY a JSON object with these exact keys: customer, rfq, ro, pn, sn, stage, summary, confidence.

For "stage" pick exactly one: RFQ/capability request, Part shipped, Quote issued, Quote approval, Repair complete, or Unclassified.
For "confidence" give an integer 0-100 representing how confident you are in the classification based on available information. Consider:
- 90-100: clear aviation MRO email with part number, RO, and obvious stage
- 70-89: aviation context clear but some details missing
- 50-69: some aviation terms but ambiguous or missing key fields
- 0-49: vague, no part numbers, no clear MRO context, or unrelated content

If confidence is below 60, set stage to Unclassified regardless of other signals.
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
        result = json.loads(raw)
        confidence = result.get("confidence", 100)
        if isinstance(confidence, int) and confidence < 60:
            result["stage"] = "Unclassified"
        result["confidence"] = confidence
        return result
    except:
        return {"stage":"Unclassified","customer":None,"ro":None,"pn":None,"sn":None,"rfq":None,"summary":None,"confidence":0}


def main():
    try:
        EMAIL    = os.environ["MRO_EMAIL"]
        PASSWORD = os.environ["MRO_APP_PASSWORD"]
        ANT_KEY  = os.environ["ANTHROPIC_KEY"]
        SA_JSON  = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])

        print(f"Project: {SA_JSON.get('project_id')}")
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
            fields["docs"]     = get_attachments(msg)

            ro_raw  = fields.get("ro")
            ro_norm = normalize_ro(ro_raw)
            customer = fields.get("customer")

            print(f"Attachments found: {fields['docs']}")

            if ro_norm and customer:
                existing = db.collection("emails")\
                    .where("customer","==",customer)\
                    .limit(10).get()

                matched = None
                for doc in existing:
                    d = doc.to_dict()
                    if normalize_ro(d.get("ro")) == ro_norm:
                        matched = doc
                        break

                if matched:
                    new_message = {
                        "dir":  "inbound",
                        "from": sender,
                        "time": fields["date"],
                        "body": body[:500]
                    }
                    matched.reference.update({
                        "messages": firestore.ArrayUnion([new_message]),
                        "stage":    fields.get("stage","Unclassified"),
                        "summary":  fields.get("summary",""),
                        "date":     fields["date"],
                        "docs":     fields["docs"],
                    })
                    print(f"Updated thread: {customer} | {ro_raw}")
                else:
                    ref = db.collection("emails").add(fields)
                    print(f"New RO saved: {customer} | {ro_raw} | {ref[1].id}")
            else:
                ref = db.collection("emails").add(fields)
                print(f"Saved (no RO): {customer} | {fields.get('stage')}")

        M.logout()
        print("Done")

    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
