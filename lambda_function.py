import os
import json
import base64
import hmac
import hashlib
import logging
import datetime
import urllib.request
import urllib.error
from urllib.parse import parse_qs, quote

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET   = "full-pipeline-cache"
LEAD_PREFIX = "guide-leads/"
FROM_ADDR   = '"Chad Gracia" <cgracia@graciagroup.com>'
REPLY_TO    = "cgracia@rainmakersecurities.com"
NOTIFY_TO   = "cgracia@rainmakersecurities.com"
THANKS_URL  = "https://pre-ipo.graciagroup.com/guide/thanks"
GUIDE_URL   = "https://pre-ipo.graciagroup.com/guide/inside-a-pre-ipo-trade"
GUIDE_SUBJ  = "Your guide: Inside a Pre-IPO Trade"

HMAC_SECRET         = os.environ.get("HMAC_SECRET", "")
PIPELINE_JWT_BUCKET = "pipeline-token"
PIPELINE_JWT_KEY    = "pipeline-jwt.json"

# ── Lead source mapping (Pipeline person field "source_id") ──────────────────
# src tags containing "linkedin" → LinkedIn; everything else (hub/deal-page
# tags, organic Google, direct, missing) → Claude SEO.
SOURCE_LINKEDIN_ID = 3349364   # "LinkedIn"
SOURCE_DEFAULT_ID  = 3630329   # "Claude SEO"

# ── Fixed custom fields set on every NEW guide lead ──────────────────────────
NEW_LEAD_CUSTOM_FIELDS = {
    "custom_label_3775335": 6613673,   # Weekly Newsletter = Confirm
    "custom_label_3075382": 6596061,   # Role = Investor
    "custom_label_3923758": 7162165,   # Investor Level = Substantive
}

DISCLAIMER = """--
DISCLOSURE: Rainmaker Securities, LLC ("RMS") is a FINRA registered broker-dealer and SIPC member. Find this broker-dealer and its agents on BrokerCheck. Our relationship summary can be found on the RMS website (https://www.rainmakersecurities.com/crs).

RMS is engaged by its clients to make referrals to buyers or sellers of private securities ("Securities"). If such client closes a Securities transaction with a buyer or seller so referred, RMS is entitled to a success fee from the client. Such success fee may be in the form of cash or in warrants to purchase securities of the client or client's affiliate. RMS or RMS representatives may hold equity in its issuer clients or in the issuers of securities purchased or sold by the parties to a transaction.

This communication is confidential and is addressed only to its intended recipient. This communication does not represent an offer or solicitation to buy or sell Securities. Such an offer must be made via definitive legal documentation by the seller of securities.

Investments in the Securities are speculative and involve a high degree of risk. An investor in the Securities should have little to no need for liquidity in the foreseeable future and have sufficient finances to withstand the loss of the entire investment.

RMS does not recommend the purchase or sale of Securities. Potential buyers or sellers of the Securities should seek professional counsel prior to entering into any transaction.

RISK FACTORS: Investments in the Securities are speculative and involve a high degree of risk. Companies engaging in private placements may be early stage and high risk. You should be able to afford the increased risk of loss with such investments, including the potential of a total loss. An investor in the Securities should have little to no need for liquidity in the foreseeable future. Unlike an investment purchased on a stock exchange, an investment in a private placement is highly illiquid. You will most likely be investing in restricted securities, may have difficulty finding a buyer for the securities when you can resell and, as a result, may need to hold the securities indefinitely. Limited disclosure: Companies engaging in private placements are not required to provide the disclosure that would be required in a registered offering. Potential buyers or sellers of the Securities should seek professional counsel prior to entering into any transaction."""


# ── Pipeline person find-or-create ────────────────────────────────────────────

def get_jwt():
    s3  = boto3.client("s3")
    obj = s3.get_object(Bucket=PIPELINE_JWT_BUCKET, Key=PIPELINE_JWT_KEY)
    return json.loads(obj["Body"].read())["jwt"]


def make_token(id_value):
    msg = str(id_value).encode()
    sig = hmac.new(HMAC_SECRET.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def call_pipeline_api(method, endpoint, payload=None, jwt=None):
    base = "https://api.pipelinecrm.com/api/v3"
    url  = f"{base}{endpoint}"
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type":  "application/json",
    }
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=data, headers=headers,
                                  method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"status": r.status, "data": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "data": e.read().decode()}
    except Exception as e:
        return {"status": 500, "data": str(e)}


def lead_source_id_for(src):
    if "linkedin" in (src or "").lower():
        return SOURCE_LINKEDIN_ID
    return SOURCE_DEFAULT_ID


def last_name_from_email(email, first_name):
    """Derive a last name only when the email's local part is clearly
    <first name or 3+ letter prefix of it><last name> (jennpearse@ +
    Jennifer -> Pearse) or <first initial><last name of 4+ letters>
    (ssilverstein@ + Sarah -> Silverstein). Returns "" when unsure."""
    local = (email.split("@")[0] or "").lower().split("+")[0]
    first = "".join(c for c in (first_name or "").lower() if c.isalpha())
    if len(first) < 3:
        return ""
    for sep in (".", "_", "-"):
        if sep in local:
            parts = [p for p in local.split(sep) if p]
            if len(parts) == 2 and first.startswith(parts[0][:3]) \
                    and parts[0] in first and parts[1].isalpha() \
                    and len(parts[1]) >= 3:
                return parts[1].capitalize()
            return ""
    if not local.isalpha():
        return ""
    for n in range(len(first), 2, -1):
        prefix = first[:n]
        if local.startswith(prefix):
            rest = local[n:]
            if len(rest) >= 3 and rest.isalpha():
                return rest.capitalize()
            return ""
    # First initial + surname, e.g. ssilverstein@ + Sarah -> Silverstein
    if local[0] == first[0] and len(local) >= 5:
        return local[1:].capitalize()
    return ""


def find_or_create_person(email, first_name, src, ts):
    """Returns (person_id or None, existed: bool)."""
    jwt = get_jwt()

    resp = call_pipeline_api(
        "GET",
        f"/people.json?conditions%5Bperson_email%5D={quote(email)}&per_page=5",
        jwt=jwt,
    )
    if resp["status"] == 200 and isinstance(resp["data"], dict):
        target = email.lower()
        for p in resp["data"].get("entries", []):
            for k in ("email", "email2", "home_email"):
                if (p.get(k) or "").strip().lower() == target:
                    return p["id"], True

    person = {
        "first_name": first_name or email.split("@")[0],
        "email": email,
        "type": "Lead",
        "source_id": lead_source_id_for(src),
        "custom_fields": dict(NEW_LEAD_CUSTOM_FIELDS),
        "summary": (
            f"Guide signup via pre-ipo.graciagroup.com on {ts}. "
            f"Source tag: {src or 'none'}."
        ),
    }
    last_name = last_name_from_email(email, first_name)
    if last_name:
        person["last_name"] = last_name
    payload = {"person": person}
    resp = call_pipeline_api("POST", "/people.json", payload=payload, jwt=jwt)
    if resp["status"] == 200 and isinstance(resp["data"], dict) \
            and resp["data"].get("id"):
        return resp["data"]["id"], False
    logger.error(f"guide-signup: person create failed: {resp}")
    return None, False


# ── Emails ────────────────────────────────────────────────────────────────────

def redirect():
    return {
        "statusCode": 302,
        "headers": {"Location": THANKS_URL},
        "body": "",
    }


def guide_link(person_id):
    if person_id:
        return f"{GUIDE_URL}#p={person_id}&t={make_token(person_id)}"
    return GUIDE_URL


def guide_email_text(first_name, link):
    name = first_name or "there"
    return (
        f"Hi {name},\n\n"
        "Thanks for requesting the guide. Read it here:\n"
        f"{link}\n\n"
        "You'll also receive my weekly pre-IPO briefing. If it's ever not "
        "useful, reply 'unsubscribe' and I'll remove you personally.\n\n"
        "Chad Gracia\n"
        "Registered Representative, Rainmaker Securities, LLC "
        "(member FINRA/SIPC)\n\n"
        + DISCLAIMER
    )


def disclaimer_html():
    return (
        "<div style='font-size:10px;color:#9ca3af;line-height:1.5;"
        "margin-top:24px;border-top:1px solid #e5e2dc;padding-top:14px;'>"
        "<p style='margin:0 0 10px 0;'>"
        + DISCLAIMER.replace("--\n", "").replace(
            "\n\n", "</p><p style='margin:0 0 10px 0;'>")
        + "</p></div>"
    )


def guide_email_html(first_name, link):
    name = first_name or "there"
    return (
        "<!DOCTYPE html><html><body style=\"margin:0;padding:24px;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif;color:#1f2937;\">"
        "<div style='max-width:560px;'>"
        f"<p style='font-size:15px;line-height:1.6;'>Hi {name},</p>"
        "<p style='font-size:15px;line-height:1.6;'>Thanks for requesting "
        "the guide. Read it here:</p>"
        f"<p style='margin:18px 0;'><a href='{link}' "
        "style='display:inline-block;background:#3d5a73;color:#ffffff;"
        "font-size:14px;font-weight:600;padding:10px 18px;border-radius:5px;"
        "text-decoration:none;'>Inside a Pre-IPO Trade &rarr;</a></p>"
        "<p style='font-size:15px;line-height:1.6;'>You'll also receive my "
        "weekly pre-IPO briefing. If it's ever not useful, reply "
        "'unsubscribe' and I'll remove you personally.</p>"
        "<p style='font-size:15px;line-height:1.6;margin-bottom:4px;'>"
        "Chad Gracia</p>"
        "<p style='font-size:12px;color:#6b7280;margin-top:0;'>Registered "
        "Representative, Rainmaker Securities, LLC (member FINRA/SIPC)</p>"
        + disclaimer_html() +
        "</div></body></html>"
    )


def store_lead(email, first_name, src, ts, person_id):
    s3 = boto3.client("s3")
    safe_email = email.replace("/", "_")
    key = f"{LEAD_PREFIX}{ts.replace(':', '-')}_{safe_email}.json"
    record = {
        "email": email,
        "first_name": first_name,
        "src": src,
        "timestamp": ts,
        "pipeline_person_id": person_id,
    }
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json",
    )


def send_guide_email(email, first_name, link):
    ses = boto3.client("ses", region_name="us-east-1")
    ses.send_email(
        Source=FROM_ADDR,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {"Data": GUIDE_SUBJ},
            "Body": {
                "Text": {"Data": guide_email_text(first_name, link)},
                "Html": {"Data": guide_email_html(first_name, link)},
            },
        },
        ReplyToAddresses=[REPLY_TO],
    )


def send_notify_email(email, first_name, src, ts, person_id, existed):
    ses = boto3.client("ses", region_name="us-east-1")
    if person_id and existed:
        pl = (f"Pipeline: EXISTING contact #{person_id} — "
              f"https://app.pipelinecrm.com/people/{person_id}")
    elif person_id:
        pl = (f"Pipeline: new person #{person_id} created — "
              f"https://app.pipelinecrm.com/people/{person_id}")
    else:
        pl = "Pipeline: person create FAILED — check guide-signup logs."
    body = (
        "New guide lead\n\n"
        f"Email: {email}\n"
        f"First name: {first_name or '(not given)'}\n"
        f"Source: {src or '(not given)'}\n"
        f"Time (UTC): {ts}\n"
        f"{pl}\n"
    )
    ses.send_email(
        Source=FROM_ADDR,
        Destination={"ToAddresses": [NOTIFY_TO]},
        Message={
            "Subject": {"Data": f"New guide lead: {email}"},
            "Body": {"Text": {"Data": body}},
        },
        ReplyToAddresses=[REPLY_TO],
    )


TEST_FORM = (
    "<!DOCTYPE html><html><body style='font-family:sans-serif;padding:32px;'>"
    "<h3>guide-signup test form</h3>"
    "<form method='POST'>"
    "<p><input name='first_name' placeholder='First name'></p>"
    "<p><input name='email' placeholder='Email'></p>"
    "<p><input name='src' placeholder='src tag' value='console-test'></p>"
    "<p><button type='submit'>Submit</button></p>"
    "</form></body></html>"
)


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method", "GET")
    ).upper()

    if method != "POST":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": TEST_FORM,
        }

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")

    params = {k: v[0] for k, v in parse_qs(body).items()}
    email      = (params.get("email") or "").strip()[:200]
    first_name = (params.get("first_name") or "").strip()[:100]
    src        = (params.get("src") or "").strip()[:100]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    if "@" not in email or "." not in email.split("@")[-1]:
        logger.info(f"guide-signup: invalid email {email!r}, redirecting")
        return redirect()

    person_id, existed = None, False
    try:
        person_id, existed = find_or_create_person(email, first_name, src, ts)
    except Exception as e:
        logger.error(f"guide-signup: pipeline failed for {email}: {e}")

    try:
        store_lead(email, first_name, src, ts, person_id)
    except Exception as e:
        logger.error(f"guide-signup: store failed for {email}: {e}")

    link = guide_link(person_id)
    try:
        send_guide_email(email, first_name, link)
    except Exception as e:
        logger.error(f"guide-signup: guide email failed for {email}: {e}")

    try:
        send_notify_email(email, first_name, src, ts, person_id, existed)
    except Exception as e:
        logger.error(f"guide-signup: notify failed for {email}: {e}")

    return redirect()