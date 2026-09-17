import os
import uuid
import hmac
import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

from flask import request, redirect, url_for, jsonify

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


def register_finance_routes(app, page_renderer, vehicles, html_text):
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

        if not schema_ok:
            database_alert = (
                "<div class='alert alert-warning'>"
                + escape(schema_error)
                + " Фінансова сторінка вже готова; потрібно лише "
                "під'єднати існуючу базу Render."
                "</div>"
            )
            form_disabled = "disabled"

        body = """
        {database_alert}
        {message}

        <div class="grid">{summary}</div>

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
