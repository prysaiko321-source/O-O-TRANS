import os
import uuid
import hmac
import hashlib
import base64
import re
from io import BytesIO
from email.utils import parseaddr
from urllib.parse import urlencode
from datetime import date
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape

from flask import request, redirect, url_for, jsonify, session
import requests

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
FINANCE_IMPORT_SECRET = os.environ.get(
    "FINANCE_IMPORT_SECRET",
    ""
).strip()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get(
    "GOOGLE_CLIENT_SECRET",
    ""
).strip()
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://o-o-trans-bot.onrender.com/finance/gmail/callback"
).strip()
GMAIL_TOKEN_KEY_SOURCE = (
    os.environ.get("SESSION_SECRET", "").strip()
    or os.environ.get("ADMIN_PASSWORD", "").strip()
)
GMAIL_SEARCH_QUERY = os.environ.get(
    "GMAIL_SEARCH_QUERY",
    (
        "newer_than:120d has:attachment "
        "{faktura invoice rechnung facture rachunek} "
        "-in:spam -in:trash"
    )
).strip()
GMAIL_SCOPES = (
    "openid email "
    "https://www.googleapis.com/auth/gmail.readonly"
)

FINANCE_CATEGORIES = {
    "transport": "Дохід за перевезення",
    "fuel": "Паливо",
    "repair": "Ремонт і сервіс",
    "tolls": "Дороги й паркінги",
    "leasing": "Лізинг або кредит",
    "insurance": "Страхування",
    "salary": "Зарплата",
    "tax": "Податки та збори",
    "office": "Офісні витрати",
    "other": "Інше"
}

ENTRY_KINDS = {
    "income": "Дохід",
    "expense": "Витрата"
}


def database_available():
    return bool(DATABASE_URL and psycopg)


def connect_database():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=8
    )


def ensure_finance_schema():
    if not database_available():
        return False, (
            "База PostgreSQL ще не підключена до Web Service "
            "через DATABASE_URL."
        )

    try:
        with connect_database() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS finance_entries (
                        id UUID PRIMARY KEY,
                        entry_kind TEXT NOT NULL,
                        entry_date DATE NOT NULL,
                        description TEXT NOT NULL,
                        category TEXT NOT NULL,
                        amount_net NUMERIC(14, 2) NOT NULL DEFAULT 0,
                        amount_vat NUMERIC(14, 2) NOT NULL DEFAULT 0,
                        amount_gross NUMERIC(14, 2) NOT NULL DEFAULT 0,
                        currency TEXT NOT NULL DEFAULT 'PLN',
                        vehicle_id TEXT,
                        contractor_name TEXT,
                        invoice_number TEXT,
                        due_date DATE,
                        payment_status TEXT NOT NULL DEFAULT 'unpaid',
                        source TEXT NOT NULL DEFAULT 'manual',
                        source_message_id TEXT,
                        attachment_name TEXT,
                        review_status TEXT NOT NULL DEFAULT 'approved',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    finance_invoice_unique
                    ON finance_entries (
                        contractor_name,
                        invoice_number,
                        amount_gross,
                        currency
                    )
                    WHERE invoice_number IS NOT NULL
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS email_invoice_queue (
                        id UUID PRIMARY KEY,
                        external_key TEXT NOT NULL UNIQUE,
                        source_message_id TEXT,
                        sender_email TEXT,
                        email_subject TEXT,
                        attachment_name TEXT,
                        invoice_date DATE,
                        due_date DATE,
                        contractor_name TEXT,
                        invoice_number TEXT,
                        description TEXT NOT NULL,
                        category TEXT NOT NULL DEFAULT 'other',
                        amount_net NUMERIC(14, 2) NOT NULL DEFAULT 0,
                        amount_vat NUMERIC(14, 2) NOT NULL DEFAULT 0,
                        amount_gross NUMERIC(14, 2) NOT NULL DEFAULT 0,
                        currency TEXT NOT NULL DEFAULT 'PLN',
                        vehicle_id TEXT,
                        payment_status TEXT NOT NULL DEFAULT 'unpaid',
                        review_status TEXT NOT NULL DEFAULT 'pending',
                        duplicate_reason TEXT,
                        imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        reviewed_at TIMESTAMPTZ,
                        finance_entry_id UUID
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS
                    email_invoice_queue_status_idx
                    ON email_invoice_queue (review_status, imported_at DESC)
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS finance_integrations (
                        provider TEXT PRIMARY KEY,
                        account_email TEXT,
                        access_token_encrypted TEXT,
                        refresh_token_encrypted TEXT,
                        token_expires_at TIMESTAMPTZ,
                        scopes TEXT,
                        status TEXT NOT NULL DEFAULT 'connected',
                        last_sync_at TIMESTAMPTZ,
                        last_sync_message TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
        return True, ""
    except Exception as exc:
        return False, f"Помилка PostgreSQL: {exc}"


def decimal_value(value):
    text = str(value or "0").strip().replace(" ", "")
    text = text.replace(",", ".")

    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def money(value, currency):
    number = decimal_value(value)
    return f"{number:,.2f} {escape(currency)}".replace(",", " ")


def clean_text(value, limit=500):
    return str(value or "").strip()[:limit]


def optional_date(value):
    text = clean_text(value, 10)
    if not text:
        return None

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def import_is_authorized():
    if not FINANCE_IMPORT_SECRET:
        return False

    supplied = request.headers.get("X-Import-Secret", "").strip()
    authorization = request.headers.get("Authorization", "").strip()

    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    return bool(
        supplied
        and hmac.compare_digest(supplied, FINANCE_IMPORT_SECRET)
    )


def make_external_key(data):
    explicit = clean_text(data.get("external_key"), 300)
    if explicit:
        return explicit

    source = "|".join([
        clean_text(data.get("source_message_id"), 300),
        clean_text(data.get("attachment_name"), 300),
        clean_text(data.get("invoice_number"), 200),
        clean_text(data.get("contractor_name"), 300),
        str(decimal_value(data.get("amount_gross"))),
        clean_text(data.get("currency"), 3).upper()
    ])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def gmail_oauth_ready():
    return bool(
        GOOGLE_CLIENT_ID
        and GOOGLE_CLIENT_SECRET
        and GMAIL_TOKEN_KEY_SOURCE
        and Fernet
    )


def token_cipher():
    if not GMAIL_TOKEN_KEY_SOURCE or not Fernet:
        return None

    digest = hashlib.sha256(
        GMAIL_TOKEN_KEY_SOURCE.encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(value):
    if not value:
        return None

    cipher = token_cipher()
    if not cipher:
        raise RuntimeError("Не налаштований ключ шифрування Gmail.")

    return cipher.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_token(value):
    if not value:
        return ""

    cipher = token_cipher()
    if not cipher:
        return ""

    try:
        return cipher.decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def get_gmail_integration():
    if not database_available():
        return None

    try:
        with connect_database() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT * FROM finance_integrations
                    WHERE provider = 'gmail'
                    LIMIT 1
                """)
                return cursor.fetchone()
    except Exception:
        return None


def store_gmail_tokens(token_data, account_email=""):
    existing = get_gmail_integration()
    refresh_token = token_data.get("refresh_token") or ""

    if not refresh_token and existing:
        refresh_encrypted = existing["refresh_token_encrypted"]
    else:
        refresh_encrypted = encrypt_token(refresh_token)

    expires_in = int(token_data.get("expires_in") or 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, expires_in - 60)
    )

    with connect_database() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO finance_integrations (
                    provider, account_email, access_token_encrypted,
                    refresh_token_encrypted, token_expires_at,
                    scopes, status, updated_at
                ) VALUES (
                    'gmail', %s, %s, %s, %s, %s, 'connected', NOW()
                )
                ON CONFLICT (provider) DO UPDATE SET
                    account_email = EXCLUDED.account_email,
                    access_token_encrypted = EXCLUDED.access_token_encrypted,
                    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                    token_expires_at = EXCLUDED.token_expires_at,
                    scopes = EXCLUDED.scopes,
                    status = 'connected',
                    updated_at = NOW()
            """, (
                account_email or (
                    existing["account_email"] if existing else None
                ),
                encrypt_token(token_data.get("access_token")),
                refresh_encrypted,
                expires_at,
                token_data.get("scope") or GMAIL_SCOPES
            ))


def gmail_access_token():
    integration = get_gmail_integration()
    if not integration or integration["status"] != "connected":
        return ""

    access_token = decrypt_token(
        integration["access_token_encrypted"]
    )
    expires_at = integration["token_expires_at"]
    now = datetime.now(timezone.utc)

    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if access_token and expires_at and expires_at > now:
        return access_token

    refresh_token = decrypt_token(
        integration["refresh_token_encrypted"]
    )
    if not refresh_token:
        return ""

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        },
        timeout=30
    )
    response.raise_for_status()
    token_data = response.json()
    token_data["refresh_token"] = refresh_token
    store_gmail_tokens(
        token_data,
        integration["account_email"] or ""
    )
    return token_data.get("access_token", "")


def gmail_request(path, access_token, params=None):
    response = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/" + path,
        headers={"Authorization": "Bearer " + access_token},
        params=params,
        timeout=35
    )
    response.raise_for_status()
    return response.json()


def gmail_header(message, name):
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def gmail_attachment_parts(part):
    found = []
    filename = clean_text(part.get("filename"), 300)
    mime_type = clean_text(part.get("mimeType"), 100).lower()
    body = part.get("body") or {}

    if filename and (
        mime_type in {
            "application/pdf",
            "application/xml",
            "text/xml",
            "image/jpeg",
            "image/png"
        }
        or filename.lower().endswith(
            (".pdf", ".xml", ".jpg", ".jpeg", ".png")
        )
    ):
        found.append({
            "filename": filename,
            "mime_type": mime_type,
            "attachment_id": body.get("attachmentId"),
            "inline_data": body.get("data"),
            "size": body.get("size") or 0
        })

    for child in part.get("parts") or []:
        found.extend(gmail_attachment_parts(child))

    return found


def decode_base64url(value):
    if not value:
        return b""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def extract_attachment_text(filename, mime_type, content):
    lower_name = filename.lower()

    if mime_type == "application/pdf" or lower_name.endswith(".pdf"):
        if not PdfReader:
            return ""
        try:
            reader = PdfReader(BytesIO(content))
            return "\n".join(
                page.extract_text() or ""
                for page in reader.pages[:20]
            )[:200000]
        except Exception:
            return ""

    if lower_name.endswith(".xml") or mime_type in {
        "application/xml",
        "text/xml"
    }:
        return content.decode("utf-8", errors="replace")[:200000]

    return ""


def parse_date_text(value):
    if not value:
        return None

    normalized = value.strip().replace("/", ".").replace("-", ".")
    parts = normalized.split(".")
    try:
        if len(parts) == 3 and len(parts[0]) == 4:
            return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
        if len(parts) == 3:
            return date(int(parts[2]), int(parts[1]), int(parts[0])).isoformat()
    except ValueError:
        return None
    return None


def first_regex(text, patterns, flags=re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return clean_text(match.group(1), 300)
    return ""


def invoice_amount(text, labels):
    label_group = "|".join(re.escape(label) for label in labels)
    matches = re.findall(
        rf"(?:{label_group})[^0-9]{{0,30}}"
        rf"([0-9][0-9 .]*[,.][0-9]{{2}})",
        text,
        re.IGNORECASE
    )
    if not matches:
        return Decimal("0.00")
    return decimal_value(matches[-1].replace(" ", ""))


def invoice_data_from_text(text, sender_name, sender_email, subject):
    compact = re.sub(r"[ \t]+", " ", text or "")
    number = first_regex(compact, [
        r"faktura(?:\s+vat)?(?:\s+nr|\s+numer)?\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
        r"invoice(?:\s+(?:no|number))?\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
        r"rechnungsnummer\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})"
    ])
    issue_date = first_regex(compact, [
        r"data wystawienia[^0-9]{0,25}(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})",
        r"issue date[^0-9]{0,25}(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})",
        r"rechnungsdatum[^0-9]{0,25}(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})"
    ])
    due_date = first_regex(compact, [
        r"termin płatności[^0-9]{0,25}(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})",
        r"due date[^0-9]{0,25}(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})",
        r"zahlbar bis[^0-9]{0,25}(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}-\d{2}-\d{2})"
    ])

    net = invoice_amount(compact, ["netto", "net amount", "net total"])
    vat = invoice_amount(compact, ["vat", "podatek vat", "mwst"])
    gross = invoice_amount(compact, [
        "brutto",
        "razem do zapłaty",
        "kwota do zapłaty",
        "gross total",
        "amount due"
    ])

    if gross == 0 and net > 0:
        gross = net + vat

    upper_text = compact.upper()
    currency = "PLN"
    for code in ("EUR", "PLN", "USD", "GBP"):
        if re.search(rf"\b{code}\b", upper_text):
            currency = code
            break

    searchable = " ".join([
        sender_name,
        sender_email,
        subject,
        compact[:20000]
    ]).lower()
    category = "other"
    description = "Фактура з Gmail"

    if any(word in searchable for word in (
        "e100", "dkv", "eurowag", "diesel", "adblue", "paliwo"
    )):
        category = "fuel"
        description = "Паливо та дорожні послуги — фактура з Gmail"
    elif any(word in searchable for word in ("leasing", "alior")):
        category = "leasing"
        description = "Лізинг — фактура з Gmail"
    elif any(word in searchable for word in ("toll", "myto", "maut")):
        category = "tolls"
        description = "Дороги та паркінги — фактура з Gmail"
    elif any(word in searchable for word in (
        "ubezpiec", "insurance", "versicherung"
    )):
        category = "insurance"
        description = "Страхування — фактура з Gmail"
    elif any(word in searchable for word in (
        "serwis", "repair", "warsztat", "naprawa"
    )):
        category = "repair"
        description = "Ремонт або сервіс — фактура з Gmail"

    return {
        "contractor_name": sender_name or sender_email,
        "invoice_number": number,
        "invoice_date": parse_date_text(issue_date),
        "due_date": parse_date_text(due_date),
        "description": description,
        "category": category,
        "amount_net": net,
        "amount_vat": vat,
        "amount_gross": gross,
        "currency": currency
    }


def queue_email_invoice(data):
    currency = clean_text(data.get("currency") or "PLN", 3).upper()
    if currency not in {"PLN", "EUR", "USD", "GBP"}:
        currency = "PLN"

    category = clean_text(data.get("category") or "other", 40)
    if category not in FINANCE_CATEGORIES:
        category = "other"

    contractor = clean_text(data.get("contractor_name"), 300)
    invoice_number = clean_text(data.get("invoice_number"), 200)
    description = clean_text(
        data.get("description") or "Фактура з Gmail",
        500
    )
    external_key = make_external_key(data)
    gross = decimal_value(data.get("amount_gross"))
    duplicate_reason = None

    with connect_database() as connection:
        with connection.cursor() as cursor:
            if invoice_number:
                cursor.execute("""
                    SELECT id
                    FROM finance_entries
                    WHERE contractor_name = %s
                      AND invoice_number = %s
                      AND amount_gross = %s
                      AND currency = %s
                    LIMIT 1
                """, (
                    contractor or None,
                    invoice_number,
                    gross,
                    currency
                ))
                if cursor.fetchone():
                    duplicate_reason = (
                        "Така фактура вже є у фінансових операціях."
                    )

            cursor.execute("""
                INSERT INTO email_invoice_queue (
                    id, external_key, source_message_id,
                    sender_email, email_subject, attachment_name,
                    invoice_date, due_date, contractor_name,
                    invoice_number, description, category,
                    amount_net, amount_vat, amount_gross,
                    currency, vehicle_id, payment_status,
                    review_status, duplicate_reason
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (external_key) DO NOTHING
                RETURNING id
            """, (
                str(uuid.uuid4()),
                external_key,
                clean_text(data.get("source_message_id"), 300) or None,
                clean_text(data.get("sender_email"), 300) or None,
                clean_text(data.get("email_subject"), 500) or None,
                clean_text(data.get("attachment_name"), 300) or None,
                optional_date(data.get("invoice_date")),
                optional_date(data.get("due_date")),
                contractor or None,
                invoice_number or None,
                description,
                category,
                decimal_value(data.get("amount_net")),
                decimal_value(data.get("amount_vat")),
                gross,
                currency,
                clean_text(data.get("vehicle_id"), 100) or None,
                "unpaid",
                "duplicate" if duplicate_reason else "pending",
                duplicate_reason
            ))
            inserted = cursor.fetchone()

    return inserted, duplicate_reason


def sync_gmail_invoices():
    access_token = gmail_access_token()
    if not access_token:
        return 0, "Gmail не підключено або потрібне повторне підключення."

    listing = gmail_request(
        "messages",
        access_token,
        {"q": GMAIL_SEARCH_QUERY, "maxResults": 50}
    )
    imported = 0
    checked = 0

    for summary in listing.get("messages") or []:
        message_id = summary.get("id")
        if not message_id:
            continue

        message = gmail_request(
            "messages/" + message_id,
            access_token,
            {"format": "full"}
        )
        sender_header = gmail_header(message, "From")
        sender_name, sender_email = parseaddr(sender_header)
        subject = gmail_header(message, "Subject")
        parts = gmail_attachment_parts(message.get("payload") or {})

        for part in parts:
            checked += 1
            if int(part.get("size") or 0) > 10 * 1024 * 1024:
                continue

            raw_data = part.get("inline_data")
            if raw_data:
                content = decode_base64url(raw_data)
            elif part.get("attachment_id"):
                attachment = gmail_request(
                    "messages/{}/attachments/{}".format(
                        message_id,
                        part["attachment_id"]
                    ),
                    access_token
                )
                content = decode_base64url(attachment.get("data"))
            else:
                continue

            extracted_text = extract_attachment_text(
                part["filename"],
                part["mime_type"],
                content
            )
            parsed = invoice_data_from_text(
                extracted_text,
                sender_name,
                sender_email,
                subject
            )
            parsed.update({
                "external_key": "gmail:{}:{}".format(
                    message_id,
                    part.get("attachment_id") or part["filename"]
                ),
                "source_message_id": message_id,
                "sender_email": sender_email,
                "email_subject": subject,
                "attachment_name": part["filename"]
            })
            inserted, _ = queue_email_invoice(parsed)
            if inserted:
                imported += 1

    sync_message = (
        f"Перевірено файлів: {checked}. Нових фактур: {imported}."
    )
    with connect_database() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE finance_integrations
                SET last_sync_at = NOW(),
                    last_sync_message = %s,
                    updated_at = NOW()
                WHERE provider = 'gmail'
            """, (sync_message,))

    return imported, sync_message


def register_finance_routes(app, page_renderer, vehicles, html_text):
    @app.route("/finance/gmail/connect")
    def finance_gmail_connect():
        schema_ok, schema_error = ensure_finance_schema()
        if not schema_ok:
            return redirect(url_for(
                "finance_dashboard",
                gmail_error=clean_text(schema_error, 150)
            ))

        if not gmail_oauth_ready():
            return redirect(url_for(
                "finance_dashboard",
                gmail_setup="1"
            ))

        state = uuid.uuid4().hex
        session["gmail_oauth_state"] = state
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": GMAIL_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state
        }
        return redirect(
            "https://accounts.google.com/o/oauth2/v2/auth?"
            + urlencode(params)
        )

    @app.route("/finance/gmail/callback")
    def finance_gmail_callback():
        expected_state = session.pop("gmail_oauth_state", "")
        received_state = request.args.get("state", "")
        code = request.args.get("code", "")

        if not (
            expected_state
            and received_state
            and hmac.compare_digest(expected_state, received_state)
            and code
        ):
            return redirect(url_for(
                "finance_dashboard",
                gmail_error="Google не підтвердив безпечний вхід."
            ))

        try:
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": GOOGLE_REDIRECT_URI
                },
                timeout=30
            )
            response.raise_for_status()
            token_data = response.json()
            access_token = token_data.get("access_token", "")
            profile = gmail_request("profile", access_token)
            account_email = clean_text(
                profile.get("emailAddress"),
                300
            )
            store_gmail_tokens(token_data, account_email)
            return redirect(url_for(
                "finance_dashboard",
                gmail_connected="1"
            ))
        except Exception as exc:
            return redirect(url_for(
                "finance_dashboard",
                gmail_error=clean_text(exc, 150)
            ))

    @app.route("/finance/gmail/sync", methods=["POST"])
    def finance_gmail_sync():
        try:
            _, sync_message = sync_gmail_invoices()
            return redirect(url_for(
                "finance_dashboard",
                gmail_synced=sync_message
            ))
        except Exception as exc:
            return redirect(url_for(
                "finance_dashboard",
                gmail_error=clean_text(exc, 150)
            ))

    @app.route("/finance/gmail/disconnect", methods=["POST"])
    def finance_gmail_disconnect():
        schema_ok, _ = ensure_finance_schema()
        if schema_ok:
            try:
                with connect_database() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            DELETE FROM finance_integrations
                            WHERE provider = 'gmail'
                        """)
            except Exception:
                pass

        return redirect(url_for(
            "finance_dashboard",
            gmail_disconnected="1"
        ))

    @app.route("/api/finance/email-invoices/import", methods=["POST"])
    def finance_email_import():
        if not import_is_authorized():
            return jsonify({
                "ok": False,
                "error": "Немає дозволу на імпорт."
            }), 401

        schema_ok, schema_error = ensure_finance_schema()
        if not schema_ok:
            return jsonify({"ok": False, "error": schema_error}), 503

        data = request.get_json(silent=True) or {}
        currency = clean_text(data.get("currency") or "PLN", 3).upper()
        if currency not in {"PLN", "EUR", "USD", "GBP"}:
            currency = "PLN"

        category = clean_text(data.get("category") or "other", 40)
        if category not in FINANCE_CATEGORIES:
            category = "other"

        contractor = clean_text(data.get("contractor_name"), 300)
        invoice_number = clean_text(data.get("invoice_number"), 200)
        description = clean_text(
            data.get("description") or "Фактура з Gmail",
            500
        )
        external_key = make_external_key(data)
        gross = decimal_value(data.get("amount_gross"))

        duplicate_reason = None
        try:
            with connect_database() as connection:
                with connection.cursor() as cursor:
                    if invoice_number:
                        cursor.execute("""
                            SELECT id
                            FROM finance_entries
                            WHERE contractor_name = %s
                              AND invoice_number = %s
                              AND amount_gross = %s
                              AND currency = %s
                            LIMIT 1
                        """, (
                            contractor or None,
                            invoice_number,
                            gross,
                            currency
                        ))
                        if cursor.fetchone():
                            duplicate_reason = (
                                "Така фактура вже є у фінансових операціях."
                            )

                    cursor.execute("""
                        INSERT INTO email_invoice_queue (
                            id, external_key, source_message_id,
                            sender_email, email_subject, attachment_name,
                            invoice_date, due_date, contractor_name,
                            invoice_number, description, category,
                            amount_net, amount_vat, amount_gross,
                            currency, vehicle_id, payment_status,
                            review_status, duplicate_reason
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s
                        )
                        ON CONFLICT (external_key) DO NOTHING
                        RETURNING id
                    """, (
                        str(uuid.uuid4()),
                        external_key,
                        clean_text(data.get("source_message_id"), 300) or None,
                        clean_text(data.get("sender_email"), 300) or None,
                        clean_text(data.get("email_subject"), 500) or None,
                        clean_text(data.get("attachment_name"), 300) or None,
                        optional_date(data.get("invoice_date")),
                        optional_date(data.get("due_date")),
                        contractor or None,
                        invoice_number or None,
                        description,
                        category,
                        decimal_value(data.get("amount_net")),
                        decimal_value(data.get("amount_vat")),
                        gross,
                        currency,
                        clean_text(data.get("vehicle_id"), 100) or None,
                        "unpaid",
                        "duplicate" if duplicate_reason else "pending",
                        duplicate_reason
                    ))
                    inserted = cursor.fetchone()

            if not inserted:
                return jsonify({
                    "ok": True,
                    "duplicate": True,
                    "message": "Цей файл уже був імпортований."
                }), 200

            return jsonify({
                "ok": True,
                "id": str(inserted["id"]),
                "review_status": (
                    "duplicate" if duplicate_reason else "pending"
                )
            }), 201
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": f"Не вдалося імпортувати фактуру: {exc}"
            }), 500

    @app.route(
        "/finance/email-invoices/<invoice_id>/approve",
        methods=["POST"]
    )
    def finance_email_approve(invoice_id):
        schema_ok, _ = ensure_finance_schema()
        if not schema_ok:
            return redirect(url_for("finance_dashboard", error="database"))

        try:
            with connect_database() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT * FROM email_invoice_queue
                        WHERE id = %s AND review_status = 'pending'
                        FOR UPDATE
                    """, (invoice_id,))
                    item = cursor.fetchone()

                    if not item:
                        return redirect(url_for(
                            "finance_dashboard",
                            error="invoice_not_pending"
                        ))

                    entry_id = str(uuid.uuid4())
                    cursor.execute("""
                        INSERT INTO finance_entries (
                            id, entry_kind, entry_date, description,
                            category, amount_net, amount_vat,
                            amount_gross, currency, vehicle_id,
                            contractor_name, invoice_number, due_date,
                            payment_status, source, source_message_id,
                            attachment_name, review_status
                        ) VALUES (
                            %s, 'expense', %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, 'gmail', %s, %s, 'approved'
                        )
                    """, (
                        entry_id,
                        item["invoice_date"] or date.today(),
                        item["description"],
                        item["category"],
                        item["amount_net"],
                        item["amount_vat"],
                        item["amount_gross"],
                        item["currency"],
                        item["vehicle_id"],
                        item["contractor_name"],
                        item["invoice_number"],
                        item["due_date"],
                        item["payment_status"],
                        item["source_message_id"],
                        item["attachment_name"]
                    ))
                    cursor.execute("""
                        UPDATE email_invoice_queue
                        SET review_status = 'approved',
                            reviewed_at = NOW(),
                            finance_entry_id = %s
                        WHERE id = %s
                    """, (entry_id, invoice_id))

            return redirect(url_for("finance_dashboard", approved="1"))
        except Exception:
            return redirect(url_for("finance_dashboard", error="duplicate"))

    @app.route(
        "/finance/email-invoices/<invoice_id>/reject",
        methods=["POST"]
    )
    def finance_email_reject(invoice_id):
        schema_ok, _ = ensure_finance_schema()
        if schema_ok:
            try:
                with connect_database() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            UPDATE email_invoice_queue
                            SET review_status = 'rejected', reviewed_at = NOW()
                            WHERE id = %s AND review_status = 'pending'
                        """, (invoice_id,))
            except Exception:
                pass

        return redirect(url_for("finance_dashboard", rejected="1"))

    @app.route("/finance", methods=["GET", "POST"])
    def finance_dashboard():
        schema_ok, schema_error = ensure_finance_schema()
        message = ""

        if request.method == "POST" and schema_ok:
            entry_kind = request.form.get("entry_kind", "expense")
            category = request.form.get("category", "other")
            currency = request.form.get("currency", "PLN").upper()

            if entry_kind not in ENTRY_KINDS:
                entry_kind = "expense"

            if category not in FINANCE_CATEGORIES:
                category = "other"

            if currency not in {"PLN", "EUR", "USD", "GBP"}:
                currency = "PLN"

            entry_date = request.form.get("entry_date") or date.today().isoformat()
            description = request.form.get("description", "").strip()
            contractor = request.form.get("contractor_name", "").strip()
            invoice_number = request.form.get("invoice_number", "").strip() or None
            vehicle_id = request.form.get("vehicle_id", "").strip() or None
            due_date = request.form.get("due_date", "").strip() or None
            amount_net = decimal_value(request.form.get("amount_net"))
            amount_vat = decimal_value(request.form.get("amount_vat"))
            amount_gross = decimal_value(request.form.get("amount_gross"))

            if not description:
                message = (
                    "<div class='alert alert-error'>"
                    "Вкажіть опис операції."
                    "</div>"
                )
            else:
                try:
                    with connect_database() as connection:
                        with connection.cursor() as cursor:
                            cursor.execute("""
                                INSERT INTO finance_entries (
                                    id, entry_kind, entry_date,
                                    description, category,
                                    amount_net, amount_vat, amount_gross,
                                    currency, vehicle_id,
                                    contractor_name, invoice_number,
                                    due_date, payment_status,
                                    source, review_status
                                ) VALUES (
                                    %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s
                                )
                            """, (
                                str(uuid.uuid4()),
                                entry_kind,
                                entry_date,
                                description,
                                category,
                                amount_net,
                                amount_vat,
                                amount_gross,
                                currency,
                                vehicle_id,
                                contractor or None,
                                invoice_number,
                                due_date,
                                request.form.get("payment_status", "unpaid"),
                                "manual",
                                "approved"
                            ))
                    return redirect(url_for("finance_dashboard", saved="1"))
                except Exception as exc:
                    message = (
                        "<div class='alert alert-error'>"
                        + escape(f"Не вдалося зберегти: {exc}")
                        + "</div>"
                    )

        if request.args.get("saved") == "1":
            message = (
                "<div class='alert alert-ok'>"
                "Операцію збережено."
                "</div>"
            )

        if request.args.get("approved") == "1":
            message = (
                "<div class='alert alert-ok'>"
                "Фактуру підтверджено та додано у витрати."
                "</div>"
            )

        if request.args.get("rejected") == "1":
            message = (
                "<div class='alert alert-warning'>"
                "Фактуру відхилено. У фінанси її не додано."
                "</div>"
            )

        if request.args.get("error"):
            message = (
                "<div class='alert alert-error'>"
                "Не вдалося виконати дію. Можливо, фактура вже оброблена "
                "або дублюється."
                "</div>"
            )

        if request.args.get("gmail_connected") == "1":
            message = (
                "<div class='alert alert-ok'>"
                "Gmail успішно підключено. Тепер можна завантажити фактури."
                "</div>"
            )

        if request.args.get("gmail_disconnected") == "1":
            message = (
                "<div class='alert alert-warning'>"
                "Gmail відключено від програми."
                "</div>"
            )

        if request.args.get("gmail_synced"):
            message = (
                "<div class='alert alert-ok'>"
                + html_text(request.args.get("gmail_synced"))
                + "</div>"
            )

        if request.args.get("gmail_setup") == "1":
            message = (
                "<div class='alert alert-warning'>"
                "Спочатку потрібно додати в Render ключі Google Gmail API."
                "</div>"
            )

        if request.args.get("gmail_error"):
            message = (
                "<div class='alert alert-error'>"
                "Помилка Gmail: "
                + html_text(request.args.get("gmail_error"))
                + "</div>"
            )

        rows = []
        email_invoices = []
        totals = {}

        if schema_ok:
            try:
                with connect_database() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            SELECT *
                            FROM finance_entries
                            ORDER BY entry_date DESC, created_at DESC
                            LIMIT 100
                        """)
                        rows = cursor.fetchall()

                        cursor.execute("""
                            SELECT
                                currency,
                                COALESCE(SUM(
                                    CASE WHEN entry_kind = 'income'
                                    THEN amount_gross ELSE 0 END
                                ), 0) AS income,
                                COALESCE(SUM(
                                    CASE WHEN entry_kind = 'expense'
                                    THEN amount_gross ELSE 0 END
                                ), 0) AS expense
                            FROM finance_entries
                            WHERE review_status = 'approved'
                            GROUP BY currency
                            ORDER BY currency
                        """)
                        for total in cursor.fetchall():
                            totals[total["currency"]] = total

                        cursor.execute("""
                            SELECT *
                            FROM email_invoice_queue
                            ORDER BY
                                CASE review_status
                                    WHEN 'pending' THEN 0
                                    WHEN 'duplicate' THEN 1
                                    ELSE 2
                                END,
                                imported_at DESC
                            LIMIT 100
                        """)
                        email_invoices = cursor.fetchall()
            except Exception as exc:
                schema_ok = False
                schema_error = f"Помилка читання PostgreSQL: {exc}"

        vehicle_names = {
            vehicle["id"]: vehicle["name"]
            for vehicle in vehicles
        }

        summary_cards = []

        for currency, total in totals.items():
            income = decimal_value(total["income"])
            expense = decimal_value(total["expense"])
            profit = income - expense
            summary_cards.append("""
                <div class="stat">
                    <div class="label">Результат у {currency}</div>
                    <div class="value">{profit}</div>
                    <div class="small">
                        Доходи: {income} · Витрати: {expense}
                    </div>
                </div>
            """.format(
                currency=escape(currency),
                profit=money(profit, currency),
                income=money(income, currency),
                expense=money(expense, currency)
            ))

        if not summary_cards:
            summary_cards.append("""
                <div class="stat">
                    <div class="label">Фінансовий результат</div>
                    <div class="value">Ще немає даних</div>
                </div>
            """)

        table_rows = []

        for row in rows:
            kind_label = ENTRY_KINDS.get(
                row["entry_kind"],
                row["entry_kind"]
            )
            table_rows.append("""
                <tr>
                    <td>{date}</td>
                    <td>{kind}</td>
                    <td>{description}<br><span class="small">{contractor}</span></td>
                    <td>{category}</td>
                    <td>{vehicle}</td>
                    <td>{gross}</td>
                    <td>{payment}</td>
                </tr>
            """.format(
                date=html_text(row["entry_date"]),
                kind=html_text(kind_label),
                description=html_text(row["description"]),
                contractor=html_text(row["contractor_name"], ""),
                category=html_text(
                    FINANCE_CATEGORIES.get(
                        row["category"],
                        row["category"]
                    )
                ),
                vehicle=html_text(
                    vehicle_names.get(row["vehicle_id"]),
                    "Вся компанія"
                ),
                gross=money(row["amount_gross"], row["currency"]),
                payment=(
                    "Оплачено"
                    if row["payment_status"] == "paid"
                    else "Не оплачено"
                )
            ))

        if not table_rows:
            table_rows.append("""
                <tr><td colspan="7">Операцій ще немає.</td></tr>
            """)

        email_rows = []
        status_labels = {
            "pending": "На перевірку",
            "approved": "Підтверджено",
            "rejected": "Відхилено",
            "duplicate": "Дублікат"
        }

        for item in email_invoices:
            actions = "—"
            if item["review_status"] == "pending":
                actions = """
                    <form method="post" action="/finance/email-invoices/{id}/approve" style="display:inline">
                        <button type="submit">Підтвердити</button>
                    </form>
                    <form method="post" action="/finance/email-invoices/{id}/reject" style="display:inline">
                        <button type="submit" style="background:#8d1717">Відхилити</button>
                    </form>
                """.format(id=escape(str(item["id"])))

            warning = ""
            if item["duplicate_reason"]:
                warning = "<br><span class='error'>{}</span>".format(
                    html_text(item["duplicate_reason"])
                )

            email_rows.append("""
                <tr>
                    <td>{date}</td>
                    <td>{contractor}<br><span class="small">{sender}</span></td>
                    <td>{number}<br><span class="small">{attachment}</span></td>
                    <td>{description}</td>
                    <td>{gross}</td>
                    <td>{status}{warning}</td>
                    <td>{actions}</td>
                </tr>
            """.format(
                date=html_text(item["invoice_date"]),
                contractor=html_text(item["contractor_name"]),
                sender=html_text(item["sender_email"], ""),
                number=html_text(item["invoice_number"]),
                attachment=html_text(item["attachment_name"], ""),
                description=html_text(item["description"]),
                gross=money(item["amount_gross"], item["currency"]),
                status=html_text(status_labels.get(
                    item["review_status"],
                    item["review_status"]
                )),
                warning=warning,
                actions=actions
            ))

        if not email_rows:
            email_rows.append("""
                <tr><td colspan="7">Фактур із пошти ще немає.</td></tr>
            """)

        vehicle_options = [
            '<option value="">Вся компанія</option>'
        ]
        for vehicle in vehicles:
            vehicle_options.append(
                '<option value="{}">{}</option>'.format(
                    escape(vehicle["id"]),
                    escape(vehicle["name"])
                )
            )

        category_options = []
        for key, label in FINANCE_CATEGORIES.items():
            category_options.append(
                '<option value="{}">{}</option>'.format(
                    escape(key),
                    escape(label)
                )
            )

        database_alert = ""
        form_disabled = ""
        gmail_block = ""

        if not schema_ok:
            database_alert = (
                "<div class='alert alert-warning'>"
                + escape(schema_error)
                + " Фінансова сторінка вже готова; потрібно лише "
                "під'єднати існуючу базу Render."
                "</div>"
            )
            form_disabled = "disabled"

        gmail_integration = get_gmail_integration() if schema_ok else None

        if gmail_integration and gmail_integration["status"] == "connected":
            gmail_block = """
            <div class="card">
                <h2>Підключення Gmail</h2>
                <div class="alert alert-ok">
                    Gmail підключено: <strong>{email}</strong>
                </div>
                <p class="small">
                    Остання перевірка: {last_sync}<br>
                    {last_message}
                </p>
                <form method="post" action="/finance/gmail/sync" style="display:inline">
                    <button type="submit">Завантажити нові фактури</button>
                </form>
                <form method="post" action="/finance/gmail/disconnect" style="display:inline">
                    <button type="submit" style="background:#8d1717">Відключити Gmail</button>
                </form>
            </div>
            """.format(
                email=html_text(gmail_integration["account_email"]),
                last_sync=html_text(gmail_integration["last_sync_at"]),
                last_message=html_text(
                    gmail_integration["last_sync_message"],
                    "Ще не перевірялося"
                )
            )
        elif gmail_oauth_ready():
            gmail_block = """
            <div class="card">
                <h2>Підключення Gmail</h2>
                <p>
                    Підключіть пошту лише для читання фактур.
                    Програма не зможе надсилати або видаляти листи.
                </p>
                <a class="button" href="/finance/gmail/connect">
                    Підключити Gmail
                </a>
            </div>
            """
        else:
            gmail_block = """
            <div class="card">
                <h2>Підключення Gmail</h2>
                <div class="alert alert-warning">
                    Модуль готовий. Для активації потрібно один раз додати
                    GOOGLE_CLIENT_ID і GOOGLE_CLIENT_SECRET у Render.
                </div>
                <p class="small">
                    Доступ буде тільки на читання листів. Програма не матиме
                    права надсилати або видаляти пошту.
                </p>
            </div>
            """

        body = """
        {database_alert}
        {message}

        <div class="grid">{summary}</div>

        <div style="height:18px"></div>
        {gmail_block}

        <div class="card">
            <h2>Додати операцію</h2>
            <form method="post">
                <div class="form-grid">
                    <p><label>Тип</label><select name="entry_kind" {disabled}>
                        <option value="expense">Витрата</option>
                        <option value="income">Дохід</option>
                    </select></p>
                    <p><label>Дата</label><input type="date" name="entry_date" value="{today}" {disabled}></p>
                    <p><label>Категорія</label><select name="category" {disabled}>{categories}</select></p>
                    <p><label>Автомобіль</label><select name="vehicle_id" {disabled}>{vehicles}</select></p>
                    <p><label>Опис</label><input name="description" required {disabled}></p>
                    <p><label>Контрагент</label><input name="contractor_name" {disabled}></p>
                    <p><label>Номер фактури</label><input name="invoice_number" {disabled}></p>
                    <p><label>Netto</label><input name="amount_net" inputmode="decimal" value="0" {disabled}></p>
                    <p><label>VAT</label><input name="amount_vat" inputmode="decimal" value="0" {disabled}></p>
                    <p><label>Brutto</label><input name="amount_gross" inputmode="decimal" value="0" {disabled}></p>
                    <p><label>Валюта</label><select name="currency" {disabled}>
                        <option>PLN</option><option>EUR</option><option>USD</option><option>GBP</option>
                    </select></p>
                    <p><label>Термін оплати</label><input type="date" name="due_date" {disabled}></p>
                    <p><label>Оплата</label><select name="payment_status" {disabled}>
                        <option value="unpaid">Не оплачено</option>
                        <option value="paid">Оплачено</option>
                    </select></p>
                </div>
                <button type="submit" {disabled}>Зберегти</button>
            </form>
        </div>

        <div class="card">
            <h2>Останні операції</h2>
            <div style="overflow-x:auto">
                <table>
                    <tr>
                        <th>Дата</th><th>Тип</th><th>Опис</th>
                        <th>Категорія</th><th>Автомобіль</th>
                        <th>Brutto</th><th>Оплата</th>
                    </tr>
                    {rows}
                </table>
            </div>
        </div>

        <div class="card">
            <h2>Фактури з пошти</h2>
            <p>
                Вкладення PDF, JPG і XML потрапляють сюди спочатку
                зі статусом «На перевірку».
            </p>
            <p class="small">
                Програма перевірятиме дублікати за контрагентом,
                номером фактури, сумою та валютою.
            </p>
            <div style="overflow-x:auto">
                <table>
                    <tr>
                        <th>Дата</th><th>Контрагент</th>
                        <th>Фактура / файл</th><th>Опис</th>
                        <th>Brutto</th><th>Статус</th><th>Дія</th>
                    </tr>
                    {email_rows}
                </table>
            </div>
        </div>
        """.format(
            database_alert=database_alert,
            message=message,
            summary="".join(summary_cards),
            gmail_block=gmail_block,
            disabled=form_disabled,
            today=date.today().isoformat(),
            categories="".join(category_options),
            vehicles="".join(vehicle_options),
            rows="".join(table_rows),
            email_rows="".join(email_rows)
        )

        return page_renderer(
            "Фінанси",
            body,
            "finance"
        )
