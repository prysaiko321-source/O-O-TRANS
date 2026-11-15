"""Private mail archive and conservative document bundles for transport orders."""
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.utils import getaddresses
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import request, session, redirect, url_for
import tenancy

INVOICE = re.compile(r'(?<![A-Z0-9])(FV|FS)[\s._/-]*(K[\s._/-]*)?(\d{1,6})[\s._/-]+(0?[1-9]|1[0-2])[\s._/-]+(202[56])(?!\d)', re.I)
ORDER = re.compile(r'(?<![A-Z0-9])(?:ZL|ZS)[\s._/-]*[A-Z0-9]+(?:[/_.-][A-Z0-9]+){1,8}', re.I)
ORDER_LABEL = re.compile(r'(?:zlecenie(?:\s+(?:spedycyjne|transportowe))?(?:\s+(?:nr|numer))?|order\s+(?:no|number))\s*[:#]?\s*([A-Z0-9]+(?:[/_.-][A-Z0-9]+){1,7})', re.I)
NIP_INVOICE = re.compile(r'^\d{10}-202[56]\d{4}-', re.I)
LOGO = re.compile(r'logo|signature|facebook|linkedin|image00[1-9]|smime|winmail', re.I)
SUPPORTED = re.compile(r'\.(pdf|xml|xlsx?|csv|docx?|zip|jpe?g|png|webp|tiff?|heic)$', re.I)


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
    text = ' '.join([item['subject'], item['snippet']] + [re.sub(r'\.[A-Za-z0-9]{2,5}$', '', f['filename']) for f in item['files']])
    orders = {m.group(0).rstrip('._-/') for m in ORDER.finditer(text)}
    orders.update(m.group(1).rstrip('._-/') for m in ORDER_LABEL.finditer(text))
    orders = {o for o in orders if '/' in o or '_' in o or '-' in o}
    invoices = {f'{m.group(1).upper()}/{"K/" if m.group(2) else ""}{int(m.group(3))}/{int(m.group(4)):02d}/{m.group(5)}' for m in INVOICE.finditer(text)}
    ksef = next((re.match(r'^\d{10}-(202[56])(\d{2})\d{2}-', f['filename']) for f in item['files'] if NIP_INVOICE.search(f['filename'])), None)
    short_invoice = re.search(r'\b(FV|FS)\s*(\d{1,6})(?!\d)', item['subject'], re.I)
    if not invoices and ksef and short_invoice:
        invoices.add(f'{short_invoice.group(1).upper()}/{int(short_invoice.group(2))}/{ksef.group(2)}/{ksef.group(1)}')
    own_accounts = own_accounts or {item['account']}
    addresses = [a.lower() for _, a in getaddresses([item['sender'], item['recipients']]) if a.lower() not in own_accounts]
    domains = {a if a.split('@')[-1] in {'gmail.com', 'wp.pl', 'onet.pl', 'o2.pl'} else a.split('@')[-1] for a in addresses if '@' in a}
    own_invoice = (item['sent'] or any(a.lower() in own_accounts for _,a in getaddresses([item['sender']]))) and not any('mawogroup.pl' in d for d in domains)
    cmr = any(re.search(r'cmr', f['filename'], re.I) for f in item['files'])
    return orders, invoices if own_invoice else set(), domains, bool(orders or own_invoice and (invoices or ksef) or cmr)


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
        tokens.extend(('invoice', number) for number in invoices)
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
            'date': group[0][1]['date'], 'text': ' '.join(json.dumps(item, ensure_ascii=False) for _,item in group).lower()})
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
                    DO UPDATE SET metadata=EXCLUDED.metadata,updated_at=now()''', (key, json.dumps(item)))
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
                        def fetch(summary):
                            msg = gmail_request('messages/' + summary['id'], token, {'format':'full'})
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
                        checked, page_token = 0, None
                        while True:
                            status('running','; '.join(summaries + [f'{account}: перевірено {checked} листів від червня 2025']))
                            params={'q':'after:2025/05/31 has:attachment -in:spam -in:trash','maxResults':100}
                            if page_token: params['pageToken']=page_token
                            listing=gmail_request('messages',token,params)
                            with ThreadPoolExecutor(max_workers=6) as executor:
                                batch=[]
                                for raw in executor.map(fetch,listing.get('messages',[])):
                                    if '2025-06-01' <= raw['date'] <= '2026-12-31': batch.append(raw)
                                    checked+=1
                                    if len(batch)>=15:
                                        save_messages(connect_database,batch);batch=[]
                                        status('running','; '.join(summaries+[f'{account}: перевірено {checked} листів від червня 2025']))
                                if batch: save_messages(connect_database,batch)
                            page_token=listing.get('nextPageToken')
                            if not page_token: break
                        summaries.append(f'{account}: перевірено {checked} листів, від червня 2025')
                    except Exception as exc:
                        summaries.append(f'{account}: потрібне повторне підключення Gmail' if 'RECONNECT' in str(exc) else f'{account}: пошук перервано, збережені результати доступні')
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
        groups=bundle_messages(messages)
        month=request.args.get('month','');query=request.args.get('q','').strip()[:200];kind=request.args.get('kind','transport');selected=request.args.get('bundle','')
        filtered=[g for g in groups if (kind=='all' or g['transport']) and (not month or g['date'].startswith(month)) and (not query or query.lower() in g['text']) and (not selected or g['id']==selected)]
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
            cards.append(f'<details class="card" {"open" if selected or len(filtered)==1 else ""}><summary><strong>{escape(title)}</strong> · {group["date"]} · листів {len(group["messages"])}</summary><p>Замовлення: {escape(orders) or "номер не розпізнано"}<br>Фактури: {escape(invoices) or "номер не розпізнано"}</p><p><a href="{escape(link,quote=True)}">Посилання на цей пакет</a></p><div style="overflow:auto"><table><thead><tr><th>Дата / напрям</th><th>Від кого / кому</th><th>Лист і документи</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')
        months=['2025-'+f'{m:02d}' for m in range(6,13)]+['2026-'+f'{m:02d}' for m in range(1,12)]
        options='<option value="">Від червня 2025 · усі місяці</option>'+''.join(f'<option value="{m}" {"selected" if month==m else ""}>{m}</option>' for m in months)
        kinds=''.join(f'<option value="{v}" {"selected" if kind==v else ""}>{label}</option>' for v,label in [('transport','Рейси: замовлення, фактури, CMR'),('all','Усі документи, включно з витратами та бухгалтерією')])
        pages=' '.join(f'<a href="{escape(url_for("finance_transport_documents",month=month,q=query,kind=kind,page=p),quote=True)}">{p}</a>' for p in range(1,(len(filtered)+39)//40+1))
        running=progress and progress['state']=='running'
        body='''{refresh}<p><a href="/finance">← Фінанси</a></p><h2>Замовлення · фактури · CMR</h2>
            <p>Від червня 2025 року. Листів: <strong>{letters}</strong> · вкладень: <strong>{files}</strong> · пакетів у вибірці: <strong>{groups}</strong>.</p>
            <p>Відкрийте пакет, щоб побачити листи із замовленням, фактурою та CMR. Кожне посилання веде до оригінального листа в потрібній скриньці Gmail.
            Документи об’єднано за точними номерами замовлень, фактур або однозначною гілкою листування. Скани без назви треба перевірити в листі; інші документи доступні через фільтр «Усі документи».</p>
            <p>{notice}</p><p>{progress}</p><form method="post"><input type="hidden" name="action" value="scan"><button {disabled}>Перевірити всі підключені пошти від червня 2025</button></form>
            <form method="get"><select name="month">{months}</select><select name="kind">{kinds}</select><input name="q" value="{query}" placeholder="Фірма, номер замовлення, фактури або CMR"><button>Знайти</button></form>
            <p>Місяць — дата першого листа в пакеті. Для пошуку конкретної фактури використайте її номер.</p>{cards}<p>Сторінки: {pages}</p>
            <details><summary>Імпорт індексу листів</summary><form method="post" enctype="multipart/form-data"><input type="file" name="archive" accept=".json" required><button>Імпортувати посилання</button></form></details>'''.format(
                refresh='<meta http-equiv="refresh" content="10">' if running else '',letters=len(messages),files=sum(len(m['files']) for m in messages.values()),groups=len(filtered),
                notice=escape(notice),progress=escape(progress['detail']) if progress else '',disabled='disabled' if running else '',months=options,kinds=kinds,query=escape(query,quote=True),cards=''.join(cards),pages=pages)
        return page_renderer('Замовлення, фактури та CMR',body,'finance')

    @app.route('/finance/sent-invoices')
    def finance_sent_invoices():
        return redirect(url_for('finance_transport_documents'))
