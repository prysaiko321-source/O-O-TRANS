import os
from html import escape

from flask import request, redirect, url_for

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MAX_LOGO_BYTES = 2 * 1024 * 1024


def branding_db_available():
    return bool(DATABASE_URL and psycopg)


def branding_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def ensure_branding_table():
    if not branding_db_available():
        return False

    with branding_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS company_branding (
                    company_id TEXT PRIMARY KEY,
                    company_name TEXT NOT NULL,
                    logo_data BYTEA,
                    logo_mime_type TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        connection.commit()
    return True


def get_company_branding(company_id, default_name):
    result = {
        "company_name": default_name,
        "has_custom_logo": False
    }
    if not branding_db_available():
        return result

    try:
        ensure_branding_table()
        with branding_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT company_name,
                           logo_data IS NOT NULL AS has_custom_logo
                    FROM company_branding
                    WHERE company_id = %s
                    """,
                    (company_id,)
                )
                row = cursor.fetchone()
    except Exception:
        return result

    if row:
        result["company_name"] = row.get("company_name") or default_name
        result["has_custom_logo"] = bool(row.get("has_custom_logo"))
    return result


def get_company_logo(company_id):
    if not branding_db_available():
        return None, None
    try:
        ensure_branding_table()
        with branding_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT logo_data, logo_mime_type
                    FROM company_branding
                    WHERE company_id = %s
                    """,
                    (company_id,)
                )
                row = cursor.fetchone()
    except Exception:
        return None, None

    if not row or not row.get("logo_data"):
        return None, None
    return bytes(row["logo_data"]), row.get("logo_mime_type") or "image/png"


def detect_logo_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_branding(company_id, company_name, logo_data, logo_mime_type):
    ensure_branding_table()
    with branding_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO company_branding (
                    company_id, company_name, logo_data,
                    logo_mime_type, updated_at
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (company_id) DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    logo_data = COALESCE(EXCLUDED.logo_data, company_branding.logo_data),
                    logo_mime_type = COALESCE(EXCLUDED.logo_mime_type, company_branding.logo_mime_type),
                    updated_at = NOW()
                """,
                (company_id, company_name, logo_data, logo_mime_type)
            )
        connection.commit()


def register_branding_routes(app, page, company_id, default_name, t):
    @app.route("/settings/branding", methods=["GET", "POST"])
    def branding_settings():
        error = ""
        if request.method == "POST":
            name = request.form.get("company_name", "").strip()
            logo_file = request.files.get("company_logo")
            logo_data = None
            logo_mime = None

            if not branding_db_available():
                error = "База даних недоступна."
            elif not name:
                error = "Вкажіть назву компанії."
            elif len(name) > 160:
                error = "Назва компанії надто довга."
            elif logo_file and logo_file.filename:
                logo_data = logo_file.read(MAX_LOGO_BYTES + 1)
                if len(logo_data) > MAX_LOGO_BYTES:
                    error = "Логотип завеликий. Максимальний розмір - 2 МБ."
                else:
                    logo_mime = detect_logo_mime(logo_data)
                    if not logo_mime:
                        error = "Дозволені формати: PNG, JPG або WebP."

            if not error:
                try:
                    save_branding(company_id, name, logo_data, logo_mime)
                except Exception as exc:
                    error = "Не вдалося зберегти брендинг: " + str(exc)
                else:
                    return redirect(url_for("branding_settings", saved="1"))

        branding = get_company_branding(company_id, default_name)
        notice = ""
        if request.args.get("saved") == "1":
            notice = '<div class="alert alert-ok">Налаштування збережено.</div>'
        if error:
            notice += '<div class="alert alert-error">{}</div>'.format(escape(error))

        logo_note = (
            "Завантажено власний логотип."
            if branding["has_custom_logo"]
            else "Використовується початковий логотип."
        )
        body = """
        {notice}
        <div class="card" style="max-width:760px">
            <h2>{branding_title}</h2>
            <p>Логотип показується у шапці та великим прозорим фоном на вході.</p>
            <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:18px 0">
                <img src="/assets/company-logo.jpg?v=branding" alt="Logo"
                     style="width:120px;height:120px;object-fit:contain;border:1px solid #dfe4e8;border-radius:12px;padding:7px;background:white">
                <div><strong>{name}</strong><p class="small">{logo_note}</p></div>
            </div>
            <form method="post" enctype="multipart/form-data">
                <p><label>{company_label}</label>
                <input name="company_name" value="{name}" maxlength="160" required></p>
                <p><label>Логотип / Logo</label>
                <input name="company_logo" type="file" accept="image/png,image/jpeg,image/webp"></p>
                <p class="small">PNG, JPG або WebP, максимум 2 МБ.</p>
                <button type="submit">Зберегти / Save</button>
            </form>
        </div>
        """.format(
            notice=notice,
            branding_title=escape(t("branding")),
            company_label=escape(t("company")),
            name=escape(branding["company_name"]),
            logo_note=escape(logo_note)
        )
        return page(t("branding"), body, "branding")
