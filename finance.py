import os
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape

from flask import request, redirect, url_for

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

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


def register_finance_routes(app, page_renderer, vehicles, html_text):
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

        rows = []
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
                Після підключення Gmail вкладення PDF, JPG і XML
                потраплятимуть сюди зі статусом «На перевірку».
            </p>
            <p class="small">
                Програма перевірятиме дублікати за контрагентом,
                номером фактури, сумою та валютою.
            </p>
        </div>
        """.format(
            database_alert=database_alert,
            message=message,
            summary="".join(summary_cards),
            disabled=form_disabled,
            today=date.today().isoformat(),
            categories="".join(category_options),
            vehicles="".join(vehicle_options),
            rows="".join(table_rows)
        )

        return page_renderer(
            "Фінанси",
            body,
            "finance"
        )
