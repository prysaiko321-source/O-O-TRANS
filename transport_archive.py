"""Private mail archive and conservative document bundles for transport orders."""
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import request, session, redirect, url_for
import tenancy

INVOICE = re.compile(r'(?<![A-Z0-9])(FV|FS)[\s._/-]*(K[\s._/-]*)?(\d{1,6})[\s._/-]+(0?[1-9]|1[0-2])[\s._/-]+(202[56])(?!\d)', re.I)
ORDER = re.compile(r'(?<![_A-Z0-9])(?:ZL|ZS)(?:[\s._/-]+|(?=\d))[A-Z0-9]+(?:[/_.-][A-Z0-9]+){1,8}', re.I)
ORDER_LABEL = re.compile(r'(?:zlecenie(?:\s+(?:spedycyjne|transportowe))?(?:\s+(?:nr|numer))?|order\s+(?:no|number))\s*[:#]?\s*([A-Z0-9]+(?:[/_.-][A-Z0-9]+){1,7})', re.I)
NIP_INVOICE = re.compile(r'^\d{10}-202[56]\d{4}-', re.I)
LOGO = re.compile(r'logo|signature|facebook|linkedin|image00[1-9]|smime|winmail', re.I)
SUPPORTED = re.compile(r'\.(pdf|xml|xlsx?|csv|docx?|zip|jpe?g|png|webp|tiff?|heic)$', re.I)


def document_search_form(search_type="invoice", query="", issue_date=""):
    """The same search entry point in Finance and the complete mail archive."""
    options = ''.join('<option value="{}" {}>{}</option>'.format(
        value, 'selected' if search_type == value else '', label)
        for value, label in [('invoice', 'За номером фактури'),
                              ('order', 'За номером зліцення'), ('date', 'За датою виставлення')])
    return """<form method="get" action="/finance/transport-documents" class="card">
        <label>Шукати документи
        <select name="search_type" onchange="this.form.querySelector('[name=q]').hidden=this.value==='date';this.form.querySelector('[name=issue_date]').hidden=this.value!=='date'">{options}</select></label>
        <input name="q" value="{query}" placeholder="Введіть номер" aria-label="Номер фактури або зліцення" {number_hidden}>
        <input type="date" name="issue_date" value="{issue_date}" aria-label="Дата виставлення фактури" {date_hidden}>
        <button type="submit">Знайти документи</button>
        <a href="/finance/transport-documents">Скинути пошук</a>
        <p class="small">Пошук за всі роки. Відкривається весь пов’язаний пакет: фактура, зліцення, CMR та інші документи.</p>
        </form>""".format(options=options, query=escape(query, quote=True),
            issue_date=escape(issue_date, quote=True), number_hidden='hidden' if search_type=='date' else '',
            date_hidden='' if search_type=='date' else 'hidden')


def canonical_number(value):
    return re.sub(r'[^A-ZА-ЯІЇЄҐ0-9]', '', str(value).upper())


def enrich_invoice_messages(messages, records):
    """Add extracted document references and issue dates, never email dates."""
    for row in records:
        account = (row.get('source_account_email') or '').strip().lower()
        message_id = row.get('source_message_id') or ''
        if not account or not message_id:
            continue
        key = hashlib.sha256((account + ':' + message_id).encode()).hexdigest()
        item = messages.setdefault(key, dict(account=account, message_id=message_id,
            date='', subject=row.get('email_subject') or '', sender=row.get('sender_email') or '',
            recipients=account, snippet='', thread_id='', sent=False, files=[]))
        name = row.get('attachment_name')
        if name and not any(f['filename']==name for f in item['files']):
            item['files'].append(dict(filename=name, size=0))
        number = row.get('invoice_number')
        is_order = bool(re.search('зліцення|замовлення', row.get('description') or '', re.I))
        field = 'known_orders' if is_order else 'known_invoices'
        if number:
            item.setdefault(field, set()).add(number)
        if row.get('customer_order_number'):
            item.setdefault('known_orders', set()).add(row['customer_order_number'])
        if row.get('invoice_date') and not is_order:
            item.setdefault('invoice_dates', set()).add(str(row['invoice_date'])[:10])
    return messages


def filter_document_groups(groups, search_type, query, issue_date):
    needle = canonical_number(query)
    if search_type == 'date':
        try:
            target = date.fromisoformat(issue_date).isoformat()
        except ValueError:
            return []
        return [g for g in groups if target in g['invoice_dates']]
    field = 'orders' if search_type == 'order' else 'invoices'
    return [g for g in groups if not needle or any(needle in canonical_number(n) for n in g[field])]


def normalize_message(raw):
    account = str(raw.get('account', '')).strip().lower()
    message_id = str(raw.get('message_id', ''))
    email_date = str(raw.get('date', ''))[:10]
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', account) or not re.fullmatch(r'[0-9a-f]{10,40}', message_id):
        raise ValueError('Invalid mail source')
    parsed = date.fromisoformat(email_date)
    if not date(2025, 6, 1) <= parsed <= date(2026, 12, 31):
        raise ValueError('Date outside archive range')
    files = []
    for item in raw.get('files', []):
        filename = str(item.get('filename', ''))[:500]
        size = int(item.get('size', 0) or 0)
        if not SUPPORTED.search(filename) or (LOGO.search(filename) and size < 50000):
            continue
        files.append({'filename': filename, 'size': size})
    result = {'account': account, 'message_id': message_id, 'date': email_date,
        'subject': str(raw.get('subject', ''))[:1000], 'sender': str(raw.get('sender', ''))[:1000],
        'recipients': str(raw.get('recipients', ''))[:3000], 'snippet': str(raw.get('snippet', ''))[:4000],
        'thread_id': str(raw.get('thread_id', ''))[:100], 'sent': bool(raw.get('sent')), 'files': files}
    key = hashlib.sha256((account + ':' + message_id).encode()).hexdigest()
    return key, result


def references(item, own_accounts=None):
    # Header, short message preview and filenames only; avoid merging entire
    # quoted mail histories with unrelated earlier transport orders.
    text = ' '.join([item['subject'], item['snippet']] + [re.sub(r'\.[A-Za-z0-9]{2,5}$', '', f['filename']) for f in item['files'] if not f['filename'].startswith('ACFrOg')])
    text = re.sub(r'\s*([/_.-])\s*', r'\1', text)
    orders = {m.group(0).rstrip('._-/') for m in ORDER.finditer(text)}
    orders.update(m.group(1).rstrip('._-/') for m in ORDER_LABEL.finditer(text))
    orders = {o for o in orders if len(o) <= 80 and re.search(r'\d',o) and ('/' in o or '_' in o or '-' in o)}
    invoices = {f'{m.group(1).upper()}/{"K/" if m.group(2) else ""}{int(m.group(3))}/{int(m.group(4)):02d}/{m.group(5)}' for m in INVOICE.finditer(text)}
    ksef = next((re.match(r'^\d{10}-(202[56])(\d{2})\d{2}-', f['filename']) for f in item['files'] if NIP_INVOICE.search(f['filename'])), None)
    short_invoice = re.search(r'\b(FV|FS)\s*(\d{1,6})(?!\d)', item['subject'], re.I)
    if not invoices and ksef and short_invoice:
        invoices.add(f'{short_invoice.group(1).upper()}/{int(short_invoice.group(2))}/{ksef.group(2)}/{ksef.group(1)}')
    own_accounts = own_accounts or {item['account']}
    addresses = [a.lower() for a in re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', item['sender']+' '+item['recipients']) if a.lower() not in own_accounts]
    domains = {a if a.split('@')[-1] in {'gmail.com', 'wp.pl', 'onet.pl', 'o2.pl'} else a.split('@')[-1] for a in addresses if '@' in a}
    sender_addresses = {a.lower() for a in re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', item['sender'])}
    own_invoice = (item['sent'] or bool(sender_addresses & own_accounts)) and not any('mawogroup.pl' in d for d in domains)
    cmr = any(re.search(r'cmr', f['filename'], re.I) for f in item['files'])
    orders.update(item.get('known_orders', ()))
    invoices = (invoices if own_invoice else set()) | set(item.get('known_invoices', ()))
    return orders, invoices, domains, bool(orders or invoices or cmr)


def bundle_messages(items):
    parents = {key: key for key in items}
    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]; key = parents[key]
        return key
    def join(a, b):
        parents[root(b)] = root(a)
    seen, refs, threads = {}, {}, {}
    own_accounts = {item['account'] for item in items.values()}
    for key, item in items.items():
        orders, invoices, domains, transport = references(item, own_accounts)
        refs[key] = (orders, invoices, domains, transport)
        tokens = []
        for order in orders:
            canonical = re.sub(r'[^A-Z0-9]', '', order.upper())
            # Domain qualifies short/common order numbers to avoid collisions
            # between different customers.
            tokens.extend(('order', domain, canonical) for domain in domains)
        sender_addresses={a.lower() for a in re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', item['sender'])}
        own_invoice=item['sent'] or bool(sender_addresses & own_accounts)
        # Our own invoice number is unique across the connected mailboxes.
        # External suppliers' numbers still require a matching counterparty.
        if own_invoice:
            tokens.extend(('own_invoice', canonical_number(number)) for number in invoices)
        else:
            tokens.extend(('invoice', domain, canonical_number(number)) for number in invoices for domain in domains)
        for token in tokens:
            if token in seen:
                join(key, seen[token])
            else:
                seen[token] = key
        if item['thread_id']:
            threads.setdefault((item['account'], item['thread_id']), []).append(key)
    for keys in threads.values():
        orders = {re.sub(r'[^A-Z0-9]', '', x.upper()) for k in keys for x in refs[k][0]}
        invoices = {x for k in keys for x in refs[k][1]}
        if len(orders) <= 1 and len(invoices) <= 1:
            for key in keys[1:]:
                join(keys[0], key)
    groups = {}
    for key, item in items.items():
        groups.setdefault(root(key), []).append((key, item))
    result = []
    for group in groups.values():
        group.sort(key=lambda x: (x[1]['date'], x[0]))
        orders = sorted({o for k, _ in group for o in refs[k][0]})
        invoices = sorted({o for k, _ in group for o in refs[k][1]})
        result.append({'id': min(k for k, _ in group), 'messages': group, 'orders': orders,
            'invoices': invoices, 'transport': any(refs[k][3] for k, _ in group),
            'invoice_dates': {d for _, item in group for d in item.get('invoice_dates', ())},
            'date': group[0][1]['date'], 'text': ' '.join(json.dumps(item, ensure_ascii=False, default=list) for _,item in group).lower()})
    return sorted(result, key=lambda g: g['date'], reverse=True)


def schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS finance_transport_mail (
        external_key TEXT PRIMARY KEY, metadata JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS finance_transport_scan (
        id INTEGER PRIMARY KEY, state TEXT NOT NULL, detail TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())''')


def save_messages(connect_database, raw_messages):
    rows = [normalize_message(item) for item in raw_messages]
    with connect_database() as connection:
        with connection.cursor() as cursor:
            schema(cursor)
            for key, item in rows:
                if not item['files']:
                    continue
                cursor.execute('''INSERT INTO finance_transport_mail (external_key,metadata)
                    VALUES (%s,%s::jsonb) ON CONFLICT (external_key)
                    DO UPDATE SET metadata=EXCLUDED.metadata,updated_at=now()''', (key, json.dumps(item).replace('\\u0000','')))
    return len(rows)


def register_transport_archive(app, page_renderer, connect_database, get_integrations,
                               access_token, gmail_request, gmail_header):
    def status(state, detail):
        with connect_database() as connection:
            with connection.cursor() as cursor:
                schema(cursor)
                cursor.execute('''INSERT INTO finance_transport_scan (id,state,detail)
                    VALUES (1,%s,%s) ON CONFLICT (id) DO UPDATE SET
                    state=EXCLUDED.state,detail=EXCLUDED.detail,updated_at=now()''', (state,detail[:5000]))
    def scan(company):
        with app.app_context(), tenancy.company_scope(company):
            summaries = []
            try:
                for integration in get_integrations():
                    account = integration['account_email']
                    try:
                        token = access_token(account)
                        if not token:
                            raise RuntimeError('GMAIL_RECONNECT_REQUIRED')
                        def mail_call(path,params):
                            nonlocal token
                            for attempt in range(5):
                                try:
                                    return gmail_request(path,token,params)
                                except Exception as exc:
                                    response=getattr(exc,'response',None)
                                    code=getattr(response,'status_code',None)
                                    if code==401:
                                        token=access_token(account)
                                    if attempt==4: raise
                                    retry_after=getattr(response,'headers',{}).get('Retry-After','')
                                    delay=min(60,int(retry_after)) if str(retry_after).isdigit() else min(30,2**(attempt+1))
                                    time.sleep(delay)
                        with connect_database() as connection:
                            with connection.cursor() as cursor:
                                cursor.execute('SELECT metadata FROM finance_transport_mail')
                                known={r['metadata']['message_id'] for r in cursor.fetchall() if r['metadata']['account']==account}
                        def fetch(summary):
                            try:
                                msg = mail_call('messages/' + summary['id'], {'format':'full'})
                            except Exception:
                                app.logger.exception('Transport mail message unavailable')
                                return None
                            files = []
                            def walk(part):
                                if part.get('filename'):
                                    files.append({'filename':part['filename'],'size':(part.get('body') or {}).get('size',0)})
                                for child in part.get('parts',[]):
                                    walk(child)
                            walk(msg.get('payload',{}))
                            sent_date = datetime.fromtimestamp(int(msg['internalDate'])/1000,timezone.utc).astimezone(ZoneInfo('Europe/Warsaw')).date().isoformat()
                            return {'account':account,'message_id':msg['id'],'date':sent_date,'thread_id':msg.get('threadId',''),
                                'subject':gmail_header(msg,'Subject'),'sender':gmail_header(msg,'From'),
                                'recipients':gmail_header(msg,'To') + '; ' + gmail_header(msg,'Cc'),
                                'snippet':msg.get('snippet',''),'sent':'SENT' in msg.get('labelIds',[]),'files':files}
                        def safe_fetch(summary):
                            try:
                                return fetch(summary)
                            except Exception:
                                app.logger.exception('Transport mail metadata unavailable')
                                return None
                        checked, failed, page_token = 0, 0, None
                        while True:
                            status('running','; '.join(summaries + [f'{account}: перевірено {checked} листів від червня 2025']))
                            params={'q':'after:2025/05/31 has:attachment -in:spam -in:trash','maxResults':100}
                            if page_token: params['pageToken']=page_token
                            listing=mail_call('messages',params)
                            pending=[s for s in listing.get('messages',[]) if s['id'] not in known]
                            checked+=len(listing.get('messages',[]))-len(pending)
                            with ThreadPoolExecutor(max_workers=6) as executor:
                                batch=[]
                                for raw in executor.map(safe_fetch,pending):
                                    if raw is None:
                                        failed+=1
                                        continue
                                    if '2025-06-01' <= raw['date'] <= '2026-12-31': batch.append(raw)
                                    checked+=1
                                    if len(batch)>=15:
                                        save_messages(connect_database,batch);batch=[]
                                        status('running','; '.join(summaries+[f'{account}: перевірено {checked} листів від червня 2025']))
                                if batch: save_messages(connect_database,batch)
                            page_token=listing.get('nextPageToken')
                            if not page_token: break
                        summaries.append(f'{account}: перевірено {checked} листів, від червня 2025; недоступних листів {failed}')
                    except Exception as exc:
                        response=getattr(exc,'response',None)
                        code=getattr(response,'status_code','')
                        reason=''
                        if response is not None:
                            try: reason=response.json().get('error',{}).get('errors',[{}])[0].get('reason','')
                            except (ValueError,KeyError,IndexError,AttributeError): pass
                        diagnostic=f'{type(exc).__name__} {code} {reason}'.strip()
                        summaries.append(f'{account}: потрібне повторне підключення Gmail' if 'RECONNECT' in str(exc) else f'{account}: пошук перервано ({diagnostic}), збережені результати доступні')
                        app.logger.exception('Transport mail account scan failed')
                status('done','; '.join(summaries) or 'Немає підключених скриньок')
            except Exception:
                app.logger.exception('Transport mail scan failed')
                status('error','Пошук перервано. Знайдені документи збережено.')

    @app.route('/finance/transport-documents',methods=['GET','POST'])
    def finance_transport_documents():
        if session.get('role')!='director': return 'Доступ лише директору',403
        notice=''
        with connect_database() as connection:
            with connection.cursor() as cursor:
                schema(cursor)
                if request.method=='POST' and request.form.get('action')=='scan':
                    cursor.execute('SELECT * FROM finance_transport_scan WHERE id=1 FOR UPDATE')
                    previous=cursor.fetchone()
                    if previous and previous['state']=='running' and (datetime.now(timezone.utc)-previous['updated_at']).total_seconds()<900:
                        return redirect(url_for('finance_transport_documents'))
                    cursor.execute("INSERT INTO finance_transport_scan (id,state,detail) VALUES (1,'running','Починаю пошук') ON CONFLICT(id) DO UPDATE SET state='running',detail=EXCLUDED.detail,updated_at=now()")
                    connection.commit()
                    threading.Thread(target=scan,args=(tenancy.company(),),daemon=True).start()
                    return redirect(url_for('finance_transport_documents'))
                if request.method=='POST':
                    upload=request.files.get('archive');content=upload.read(16*1024*1024+1) if upload else b''
                    if not content or len(content)>16*1024*1024: return 'Некоректний файл',400
                    try:
                        raw=json.loads(content)['messages']
                        if not isinstance(raw,list) or len(raw)>20000: raise ValueError()
                        for item in raw: normalize_message(item)
                    except (ValueError,KeyError,TypeError): return 'Некоректний файл',400
                    connection.commit()
                    notice=f'Опрацьовано листів: {save_messages(connect_database,raw)}. Повторний імпорт не створює дублів.'
                cursor.execute('SELECT external_key,metadata FROM finance_transport_mail')
                messages={r['external_key']:r['metadata'] for r in cursor.fetchall()}
                cursor.execute('SELECT * FROM finance_transport_scan WHERE id=1');progress=cursor.fetchone()
                cursor.execute("SELECT to_regclass('email_invoice_queue') AS name")
                if cursor.fetchone()['name']:
                    cursor.execute("SELECT source_account_email, source_message_id, email_subject, sender_email, attachment_name, invoice_number, customer_order_number, invoice_date, description FROM email_invoice_queue WHERE document_group <> 'accounting'")
                    enrich_invoice_messages(messages, cursor.fetchall())
        groups=bundle_messages(messages)
        month=request.args.get('month','');query=request.args.get('q','').strip()[:200];kind=request.args.get('kind','transport');selected=request.args.get('bundle','')
        search_type=request.args.get('search_type', '')
        issue_date=request.args.get('issue_date', '')
        source_message=request.args.get('source_message', '')
        source_account=request.args.get('source_account', '').strip().lower()
        active_search=search_type in {'invoice', 'order', 'date'}
        if active_search:
            groups=filter_document_groups(groups, search_type, query, issue_date)
            month='';kind='all'
            query_filter=''
        else:
            query_filter=query
        if source_message:
            groups=[g for g in groups if any(m['message_id']==source_message and m['account']==source_account for _,m in g['messages'])]
            kind='all'
        filtered=[g for g in groups if (kind=='all' or g['transport']) and (not month or g['date'].startswith(month)) and (not query_filter or query_filter.lower() in g['text']) and (not selected or g['id']==selected)]
        try: page=max(1,int(request.args.get('page','1')))
        except ValueError: page=1
        shown=filtered[(page-1)*40:page*40]
        cards=[]
        for group in shown:
            orders=' · '.join(group['orders']);invoices=' · '.join(group['invoices'])
            title=orders or invoices or group['messages'][0][1]['subject'] or 'Документи без номера'
            rows=[]
            for key,item in group['messages']:
                source='https://mail.google.com/mail/u/?'+urlencode({'authuser':item['account']})+'#all/'+item['message_id']
                filenames=[]
                for f in item['files']:
                    name=f['filename'];tag='CMR' if re.search('cmr',name,re.I) else 'Фактура' if INVOICE.search(name) or NIP_INVOICE.search(name) else 'Замовлення' if ORDER.search(name) or re.search('zlecen|order',name,re.I) else 'Скан / фото' if re.search(r'\.(jpe?g|png|webp|heic|tiff?)$',name,re.I) else 'Документ'
                    filenames.append(f'<li><strong>{tag}</strong>: <a target="_blank" rel="noopener noreferrer" href="{escape(source,quote=True)}">{escape(name)}</a></li>')
                rows.append(f'<tr><td>{item["date"]}<br>{"Відправлено" if item["sent"] else "Отримано"}</td><td>{escape(item["sender"])}<br>→ {escape(item["recipients"])}<br><small>{escape(item["account"])}</small></td><td>{escape(item["subject"])}<ul>{"".join(filenames)}</ul></td></tr>')
            link=url_for('finance_transport_documents',bundle=group['id'],kind='all')
            issue_dates=' · '.join(sorted(group['invoice_dates'])) or 'не розпізнано'
            cmr_status='CMR знайдено' if any(re.search('cmr', f['filename'], re.I) for _,m in group['messages'] for f in m['files']) else 'CMR не розпізнано — перевірте скани у пакеті'
            cards.append(f'<details class="card" {"open" if selected or active_search or source_message or len(filtered)==1 else ""}><summary><strong>{escape(title)}</strong> · {group["date"]} · листів {len(group["messages"])}</summary><p>Замовлення: {escape(orders) or "номер не розпізнано"}<br>Фактури: {escape(invoices) or "номер не розпізнано"}<br>Дата виставлення фактури: {escape(issue_dates)}<br>{cmr_status}</p><p><a href="{escape(link,quote=True)}">Посилання на цей пакет</a></p><div style="overflow:auto"><table><thead><tr><th>Дата / напрям</th><th>Від кого / кому</th><th>Лист і документи</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')
        months=['2025-'+f'{m:02d}' for m in range(6,13)]+['2026-'+f'{m:02d}' for m in range(1,12)]
        options='<option value="">Від червня 2025 · усі місяці</option>'+''.join(f'<option value="{m}" {"selected" if month==m else ""}>{m}</option>' for m in months)
        kinds=''.join(f'<option value="{v}" {"selected" if kind==v else ""}>{label}</option>' for v,label in [('transport','Рейси: замовлення, фактури, CMR'),('all','Усі документи, включно з витратами та бухгалтерією')])
        pages=' '.join(f'<a href="{escape(url_for("finance_transport_documents",month=month,q=query,kind=kind,page=p,search_type=search_type,issue_date=issue_date,source_message=source_message,source_account=source_account),quote=True)}">{p}</a>' for p in range(1,(len(filtered)+39)//40+1))
        running=progress and progress['state']=='running'
        body='''{refresh}<p><a href="/finance">← Фінанси</a></p><h2>Замовлення · фактури · CMR</h2>{search_form}
            <p>Від червня 2025 року. Листів: <strong>{letters}</strong> · вкладень: <strong>{files}</strong> · пакетів у вибірці: <strong>{groups}</strong>.</p>
            <p>Відкрийте пакет, щоб побачити листи із замовленням, фактурою та CMR. Кожне посилання веде до оригінального листа в потрібній скриньці Gmail.
            Документи об’єднано за точними номерами замовлень, фактур або однозначною гілкою листування. Скани без назви треба перевірити в листі; інші документи доступні через фільтр «Усі документи».</p>
            <p>{notice}</p><p>{progress}</p><form method="post"><input type="hidden" name="action" value="scan"><button {disabled}>Перевірити всі підключені пошти від червня 2025</button></form>
            <details><summary>Додаткові фільтри архіву</summary><form method="get"><select name="month">{months}</select><select name="kind">{kinds}</select><input name="q" value="{query}" placeholder="Фірма, номер замовлення, фактури або CMR"><button>Знайти</button></form>
            <p>Місяць — дата першого листа в пакеті. Для пошуку конкретної фактури використайте її номер.</p></details>{cards}<p>Сторінки: {pages}</p>
            <details><summary>Імпорт індексу листів</summary><form method="post" enctype="multipart/form-data"><input type="file" name="archive" accept=".json" required><button>Імпортувати посилання</button></form></details>'''.format(
                refresh='<meta http-equiv="refresh" content="10">' if running else '',letters=len(messages),files=sum(len(m['files']) for m in messages.values()),groups=len(filtered),
                search_form=document_search_form(search_type or 'invoice',query,issue_date),notice=escape(notice),progress=escape(progress['detail']) if progress else '',disabled='disabled' if running else '',months=options,kinds=kinds,query=escape(query,quote=True),cards=''.join(cards) or '<p>Документів за цим запитом не знайдено. Перевірте номер або дату. Документи без розпізнаної дати виставлення не потрапляють у пошук за датою.</p>',pages=pages)
        return page_renderer('Замовлення, фактури та CMR',body,'finance')

    @app.route('/finance/sent-invoices')
    def finance_sent_invoices():
        return redirect(url_for('finance_transport_documents'))
