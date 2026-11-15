"""Document search must retain complete bundles and use invoice issue dates."""
import copy
import unittest
from contextlib import nullcontext
from datetime import date
from flask import Flask
from transport_archive import (bundle_messages, normalize_message, enrich_invoice_messages,
    filter_document_groups, register_transport_archive, document_search_form)


def mail(number, subject, filename, day='2026-01-22', partner='client@example.com'):
    return normalize_message(dict(account='office@trans.test', message_id=f'{number:012x}',
        date=day, subject=subject, sender=partner, recipients='office@trans.test',
        snippet='', thread_id='', sent=False, files=[dict(filename=filename, size=1000)]))


class DocumentSearch(unittest.TestCase):
    def setUp(self):
        self.messages = dict([mail(1, 'Order no ABC/123/26', 'order.pdf'),
            mail(2, 'Order no ABC/123/26', 'CMR.pdf', '2026-01-25'),
            mail(3, 'Other shipment', 'invoice.pdf')])
        self.records = [dict(source_account_email='office@trans.test', source_message_id=f'{1:012x}',
            invoice_number='FV/12/01/2026', customer_order_number='ABC/123/26',
            invoice_date=date(2026,1,20), description='Фактура', attachment_name='invoice.pdf')]

    def groups(self):
        return bundle_messages(enrich_invoice_messages(copy.deepcopy(self.messages), self.records))

    def test_invoice_search_returns_order_and_cmr_from_other_days(self):
        result=filter_document_groups(self.groups(), 'invoice', 'fv 12-01-2026', '')
        self.assertEqual(len(result),1)
        self.assertEqual(len(result[0]['messages']),2)
        self.assertIn('CMR.pdf', [f['filename'] for _,m in result[0]['messages'] for f in m['files']])

    def test_order_search_returns_same_complete_package(self):
        result=filter_document_groups(self.groups(), 'order', 'abc/123/26', '')
        self.assertEqual(result[0]['invoices'], ['FV/12/01/2026'])
        self.assertEqual(len(result[0]['messages']),2)

    def test_issue_date_not_email_date(self):
        self.assertEqual(len(filter_document_groups(self.groups(),'date','','2026-01-20')),1)
        self.assertEqual(filter_document_groups(self.groups(),'date','','2026-01-22'),[])
        self.assertEqual(filter_document_groups(self.groups(),'date','','2026-02-31'),[])

    def test_same_order_number_from_another_customer_not_merged(self):
        key,item=mail(4,'Order no ABC/123/26','order.pdf',partner='someone@other.test')
        self.messages[key]=item
        self.assertEqual(len(filter_document_groups(self.groups(),'order','ABC/123/26','')),2)

    def test_invoice_missing_from_mail_index_is_searchable(self):
        self.records[0]['source_message_id']=f'{9:012x}'
        self.records[0]['sender_email']='client@example.com'
        result=filter_document_groups(self.groups(),'invoice','FV/12/01/2026','')
        self.assertEqual(len(result),1)
        self.assertEqual(len(result[0]['messages']),3)

    def test_form_has_three_modes_and_escapes_input(self):
        result=document_search_form('order','"><script>')
        for name in ['За номером фактури','За номером зліцення','За датою виставлення']:
            self.assertIn(name,result)
        self.assertNotIn('<script>',result)

    def test_route_search_not_limited_to_latest_100_and_source_links(self):
        records=copy.deepcopy(self.records)
        for n in range(100,250):
            records.append(dict(source_account_email='office@trans.test',source_message_id=f'{n:012x}',
                invoice_number=f'OTHER/{n}',invoice_date=date(2026,11,15), description='Фактура'))
        messages=self.messages
        class Cursor:
            def execute(self,sql,*args): self.sql=sql
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def fetchone(self): return {'name':'email_invoice_queue'} if 'to_regclass' in self.sql else None
            def fetchall(self):
                if 'finance_transport_mail' in self.sql:
                    return [dict(external_key=k,metadata=copy.deepcopy(v)) for k,v in messages.items()]
                return records
        class Connection:
            def cursor(self): return Cursor()
        app=Flask(__name__);app.secret_key='test'
        register_transport_archive(app,lambda title,body,section:body,
            lambda:nullcontext(Connection()),None,None,None,None)
        client=app.test_client()
        with client.session_transaction() as session: session['role']='director'
        for query in ['search_type=invoice&q=FV12-01-2026',
                      'search_type=date&issue_date=2026-01-20',
                      'search_type=order&q=ABC12326',
                      f'source_message={1:012x}&source_account=office@trans.test']:
            response=client.get('/finance/transport-documents?'+query)
            self.assertEqual(response.status_code,200,response.data)
            body=response.get_data(as_text=True)
            self.assertIn('CMR.pdf',body)
            self.assertIn('FV/12/01/2026',body)
            self.assertNotIn('OTHER/249',body)
        with client.session_transaction() as session: session['role']='driver'
        self.assertEqual(client.get('/finance/transport-documents').status_code,403)

if __name__=='__main__': unittest.main()
