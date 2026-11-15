"""Private, tenant-scoped index of original email attachments (no ledger writes)."""
import hashlib
import json
import re
from html import escape
from urllib.parse import urlencode

from flask import request, session, redirect, url_for


def normalize_document(raw):
    account = str(raw.get('account', '')).strip().lower()
    message_id = str(raw.get('message_id', '')).strip()
    filename = str(raw.get('filename', '')).strip()[:500]
    date = str(raw.get('date', ''))[:10]
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', account):
        raise ValueError('Некоректна адреса пошти')
    if not re.fullmatch(r'[0-9a-f]{10,40}', message_id):
        raise ValueError('Некоректний ідентифікатор листа')
    if not filename or not re.fullmatch(r'2026-\d{2}-\d{2}', date):
        raise ValueError('Некоректна назва або дата документа')
    key = hashlib.sha256('\0'.join((account, message_id, filename,
        str(raw.get('attachment_id', '')))).encode()).hexdigest()
    return (key, account, message_id, filename, date,
        str(raw.get('subject', ''))[:1000], str(raw.get('sender', ''))[:500])


def register_mail_archive(app, page_renderer, connect_database):
    from finance import get_gmail_integrations, gmail_access_token, gmail_request, gmail_header
    from transport_archive import register_transport_archive
    register_transport_archive(app, page_renderer, connect_database,
        get_gmail_integrations, gmail_access_token, gmail_request, gmail_header)
    @app.route('/finance/mail-archive', methods=['GET', 'POST'])
    def finance_mail_archive():
        if session.get('role') != 'director':
            return 'Доступ лише директору', 403
        notice = ''
        try:
            with connect_database() as connection:
                with connection.cursor() as cursor:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS finance_mail_archive (
                        external_key TEXT PRIMARY KEY, account TEXT NOT NULL,
                        message_id TEXT NOT NULL, filename TEXT NOT NULL,
                        email_date DATE NOT NULL, subject TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        imported_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
                    if request.method == 'POST':
                        upload = request.files.get('archive')
                        if not upload:
                            raise ValueError('Оберіть файл індексу')
                        content = upload.read(8 * 1024 * 1024 + 1)
                        if len(content) > 8 * 1024 * 1024:
                            raise ValueError('Файл завеликий')
                        documents = json.loads(content).get('documents')
                        if not isinstance(documents, list) or len(documents) > 20000:
                            raise ValueError('Некоректний список документів')
                        rows = [normalize_document(item) for item in documents]
                        added = 0
                        for row in rows:
                            cursor.execute('''INSERT INTO finance_mail_archive
                                (external_key, account, message_id, filename, email_date, subject, sender)
                                VALUES (%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (external_key) DO NOTHING''', row)
                            added += cursor.rowcount
                        notice = f'Додано документів: {added}. Уже були в архіві: {len(rows) - added}.'
                    cursor.execute('SELECT count(*) AS total FROM finance_mail_archive')
                    total = cursor.fetchone()['total']
                    cursor.execute("SELECT to_char(email_date, 'YYYY-MM') AS month, count(*) AS count FROM finance_mail_archive GROUP BY 1 ORDER BY 1")
                    months = cursor.fetchall()
                    month = request.args.get('month', '')
                    query = request.args.get('q', '').strip()[:200]
                    conditions, args = [], []
                    if re.fullmatch(r'2026-\d{2}', month):
                        conditions.append("to_char(email_date, 'YYYY-MM') = %s")
                        args.append(month)
                    if query:
                        conditions.append('(filename ILIKE %s OR subject ILIKE %s OR sender ILIKE %s)')
                        args.extend(['%' + query + '%'] * 3)
                    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
                    cursor.execute('SELECT * FROM finance_mail_archive' + where + ' ORDER BY email_date DESC, message_id, filename', args)
                    items = cursor.fetchall()
        except Exception:
            app.logger.exception('Mail archive operation failed')
            return page_renderer('Архів документів', '<p>Не вдалося відкрити або імпортувати архів. Перевірте файл і спробуйте ще раз.</p>', 'finance'), 400
        options = '<option value="">Усі місяці</option>' + ''.join(
            '<option value="{0}" {1}>{0} · {2}</option>'.format(escape(m['month']),
                'selected' if m['month'] == month else '', m['count']) for m in months)
        rows_html = []
        for item in items:
            source = 'https://mail.google.com/mail/u/?' + urlencode({'authuser': item['account']}) + '#all/' + item['message_id']
            rows_html.append('<tr><td>{}</td><td><a href="{}" target="_blank" rel="noopener noreferrer">{}</a><br><small>{}</small></td><td>{}<br><small>{}</small></td></tr>'.format(
                item['email_date'], escape(source, quote=True), escape(item['filename']),
                escape(item['subject']), escape(item['sender']), escape(item['account'])))
        body = '''<p><a href="/finance">← Фінанси</a> · <a href="/finance/transport-documents">Замовлення · фактури · CMR · від червня 2025</a></p>
            <h2>Архів документів з пошти · 2026</h2>
            <p>Усього документів: <strong>{total}</strong>. Показано: {shown}.</p>
            <p>Натисніть назву документа, щоб відкрити оригінальний лист у Gmail.
            Дата — дата листа. Архів не змінює суми доходів і витрат.</p>
            <p>{notice}</p><form method="get"><select name="month">{options}</select>
            <input name="q" value="{query}" placeholder="Назва, фактура або відправник">
            <button>Знайти</button></form>
            <div style="overflow:auto"><table><thead><tr><th>Дата листа</th><th>Документ і лист</th><th>Відправник / пошта</th></tr></thead><tbody>{rows}</tbody></table></div>
            <details><summary>Імпорт індексу документів</summary>
            <form method="post" enctype="multipart/form-data"><input type="file" name="archive" accept="application/json,.json" required><button>Імпортувати посилання</button></form></details>'''.format(
                total=total, shown=len(items), notice=escape(notice), options=options,
                query=escape(query, quote=True), rows=''.join(rows_html))
        return page_renderer('Архів документів', body, 'finance')
