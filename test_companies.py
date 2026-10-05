"""Isolation regression tests; uses local test state and mocks external GPS/SQL connections."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock

_test_dir=tempfile.TemporaryDirectory()
os.environ['DELIVERY_ROUTES_FILE']=_test_dir.name+'/legacy_routes.json'
os.environ['DOCUMENTS_DIR']=_test_dir.name+'/documents'
os.environ['DATABASE_URL']=''
os.environ['SESSION_SECRET']='local-test-session-secret-not-a-real-credential'
os.environ['ADMIN_USER']='legacy'
os.environ['ADMIN_PASSWORD']='test-legacy-password'
import app as module
import tenancy
import gps_providers
import finance
from werkzeug.security import generate_password_hash
from pypdf import PdfWriter

class Companies(unittest.TestCase):
 def setUp(self):
  self.a=module.app.test_client(); self.b=module.app.test_client()
  self.seed={'companies':{'a':{'id':'a','name':'Alpha','nip':'A','gps':{'provider':'manual'},'vehicles':[{'id':'va','plate':'AA111','name':'Alpha Van','latitude':52,'longitude':19}]},'b':{'id':'b','name':'Beta','nip':'B','gps':{'provider':'manual'},'vehicles':[{'id':'vb','plate':'BB222','name':'Beta Van','latitude':51,'longitude':20}]}},'users':{'alpha':{'id':'ua','company_id':'a','role':'director','name':'Alpha','login':'alpha','password_hash':generate_password_hash('test-password-a'),'enabled':True},'beta':{'id':'ub','company_id':'b','role':'director','name':'Beta','login':'beta','password_hash':generate_password_hash('test-password-b'),'enabled':True}}}
  with module.TENANT_ACCOUNTS_LOCK:module._write_tenant_accounts(self.seed)
  self.login(self.a,'alpha','test-password-a'); self.login(self.b,'beta','test-password-b')
 def login(self,client,name,password):
  r=client.post('/company/login',data={'login':name,'password':password})
  self.assertEqual(r.status_code,302)
 def csrf(self,client):
  client.get('/')
  with client.session_transaction() as s:return s['tenant_csrf']
 def test_all_director_sections(self):
  for path in ['/','/vehicles','/gps','/history','/fuel','/tachograph','/finance','/documents','/road-payments','/settings/branding','/driver-settings','/driver-access','/health','/company/users','/company/gps/settings']:
   with self.subTest(path=path):
    r=self.a.get(path)
    self.assertEqual(r.status_code,200, r.data[-1000:])
    text=r.get_data(as_text=True)
    self.assertNotIn('BB222',text)
    self.assertIn('Alpha',text)
  text=self.a.get('/').get_data(as_text=True)
  for url in ['/history','/finance','/documents','/driver-access','/settings/branding']:self.assertIn('href="'+url+'"',text)
 def test_company_registration(self):
  client=module.app.test_client()
  fields={'company_name':'Gamma','company_tax_id':'GAMMA','director_name':'Director','company_director_login':'gamma','company_new_password':'new-password','company_new_password2':'new-password'}
  result=client.post('/company/register',data=fields)
  self.assertEqual(result.status_code,302)
  self.assertEqual(client.get('/',follow_redirects=True).status_code,200)
  self.assertIn('Gamma',client.get('/').get_data(as_text=True))
  with module.TENANT_ACCOUNTS_LOCK:
   data=module._load_tenant_accounts();self.assertNotEqual(data['users']['gamma']['password_hash'],'new-password')
 def test_empty_company(self):
  with module.TENANT_ACCOUNTS_LOCK:
   data=module._load_tenant_accounts();data['companies']['a']['vehicles']=[];module._write_tenant_accounts(data)
  for path in ['/','/vehicles','/gps','/history','/fuel','/tachograph']:
   with self.subTest(path=path):self.assertEqual(self.a.get(path).status_code,200)
 def test_routes_state_isolation_and_restart(self):
  for client,company_id,vehicle_id,other in [(self.a,'a','va','vb'),(self.b,'b','vb','va')]:
   with module.app.test_request_context('/'):
    with tenancy.company_scope(self.seed['companies'][company_id]):
     module._write_delivery_routes({vehicle_id:{'stops':[{'address':company_id+' private address'}]}})
     module._write_driver_settings({'show_other_vehicles':company_id=='a'})
     module._write_messages([{'text':company_id+' private message'}])
     self.assertNotIn(other,module._load_delivery_routes())
  with module.app.test_request_context('/'):
   with tenancy.company_scope(self.seed['companies']['a']):self.assertIn('va',module._load_delivery_routes());self.assertTrue(module.driver_can_see_other_vehicles())
   with tenancy.company_scope(self.seed['companies']['b']):self.assertNotIn('va',module._load_delivery_routes());self.assertFalse(module.driver_can_see_other_vehicles())
  self.assertFalse(Path(module.DELIVERY_ROUTES_FILE).exists())
 def test_vehicle_and_api_id_boundaries(self):
  self.assertEqual(self.a.get('/vehicle/vb').status_code,404)
  self.assertEqual(self.a.get('/api/driver-gps/vb').status_code,404)
  self.assertEqual(self.a.get('/history?vehicle=vb').status_code,404)
  result=self.a.get('/api/live-vehicle-states').get_json()
  self.assertEqual([v['id'] for v in result['vehicles']],['va'])
 def test_csrf_and_fleet_permissions(self):
  self.assertEqual(self.a.post('/company/vehicles',data={'plate':'AA3'}).status_code,400)
  self.assertEqual(self.a.post('/company/vehicles',data={'plate':'AA3','csrf':self.csrf(self.a)}).status_code,302)
  self.assertIn('AA3',self.a.get('/vehicles').get_data(as_text=True))
  self.assertNotIn('AA3',self.b.get('/vehicles').get_data(as_text=True))
 def test_user_driver_and_dispatcher_access(self):
  token=self.csrf(self.a)
  r=self.a.post('/company/users',data={'name':'Driver','login':'driver-a','password':'driver-password','role':'driver','vehicle_id':'va','csrf':token})
  self.assertEqual(r.status_code,200)
  driver=module.app.test_client();self.login(driver,'driver-a','driver-password')
  self.assertEqual(driver.get('/driver').status_code,200)
  self.assertEqual(driver.get('/finance').status_code,302)
  self.assertEqual(driver.get('/driver-access').status_code,302)
  self.assertEqual(driver.get('/api/driver-gps/vb').status_code,404)
  self.a.post('/company/users',data={'name':'Logistician','login':'log-a','password':'log-password','role':'dispatcher','csrf':token})
  log=module.app.test_client();self.login(log,'log-a','log-password')
  self.assertEqual(log.get('/gps').status_code,200)
  self.assertEqual(log.get('/documents').status_code,200)
  self.assertEqual(log.get('/finance').status_code,302)
  self.assertEqual(log.post('/company/vehicles',data={'plate':'XX','csrf':self.csrf(log)}).status_code,403)
 def test_assigned_driver_cannot_change_other_vehicle_route(self):
  with module.TENANT_ACCOUNTS_LOCK:
   data=module._load_tenant_accounts();data['companies']['a']['vehicles'].append({'id':'va2','plate':'AA2','name':'Second Van'});module._write_tenant_accounts(data)
  self.a.post('/company/users',data={'name':'Driver','login':'driver-a','password':'driver-password','role':'driver','vehicle_id':'va2','csrf':self.csrf(self.a)})
  driver=module.app.test_client();self.login(driver,'driver-a','driver-password')
  headers={'X-CSRF-Token':self.csrf(driver)}
  self.assertEqual(driver.get('/api/delivery-route/va').status_code,404)
  self.assertEqual(driver.delete('/api/delivery-route/va',headers=headers).status_code,404)
  self.assertEqual(driver.get('/api/delivery-route/va2').status_code,200)
  with module.app.test_request_context('/'):
   with tenancy.company_scope(data['companies']['a']):
    module.session.update(logged_in=True,role='driver',driver_vehicle_id='va2')
    self.assertEqual(module._message_identity()[0],'vehicle:va2')
 def test_disabled_user_and_legacy_login_switch(self):
  token=self.csrf(self.a)
  self.a.post('/company/users',data={'name':'Driver','login':'driver-a','password':'driver-password','role':'driver','vehicle_id':'va','csrf':token})
  driver=module.app.test_client();self.login(driver,'driver-a','driver-password')
  with module.TENANT_ACCOUNTS_LOCK:
   data=module._load_tenant_accounts();data['users']['driver-a']['enabled']=False;module._write_tenant_accounts(data)
  self.assertEqual(driver.get('/driver').status_code,302)
  self.a.post('/login/director',data={'username':'legacy','password':'test-legacy-password'})
  with self.a.session_transaction() as session:self.assertNotIn('tenant_company_id',session)
 def test_document_file_isolation(self):
  writer=PdfWriter();writer.add_blank_page(width=200,height=200);data=io.BytesIO();writer.write(data)
  r=self.a.post('/api/documents',data={'vehicle_id':'va','type':'cmr','file':(io.BytesIO(data.getvalue()),'test.pdf')},headers={'X-CSRF-Token':self.csrf(self.a)},content_type='multipart/form-data')
  self.assertEqual(r.status_code,201,r.data)
  doc_id=r.get_json()['document']['id']
  self.assertEqual(len(self.a.get('/api/documents').get_json()['documents']),1)
  self.assertEqual(len(self.b.get('/api/documents').get_json()['documents']),0)
  self.assertEqual(self.b.get('/api/documents/'+doc_id+'/file').status_code,404)
  response=self.a.get('/api/documents/'+doc_id+'/file');self.assertEqual(response.status_code,200);response.close()
 def test_rendered_javascript(self):
  import re, subprocess
  for path in ['/','/gps','/driver-settings','/documents','/finance']:
   html=self.a.get(path).get_data(as_text=True)
   scripts=re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',html,re.S|re.I)
   for script in scripts:
    if not script.strip():continue
    codefile=Path(_test_dir.name)/'syntax.js';codefile.write_text(script)
    result=subprocess.run(['node','--check',str(codefile)],capture_output=True,text=True)
    self.assertEqual(result.returncode,0,path+': '+result.stderr)
 def test_sql_namespace_no_public_fallback(self):
  connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
  with patch('psycopg.connect',return_value=connection):
   with tenancy.company_scope(self.seed['companies']['a']):
    first=tenancy.schema_name();tenancy.scoped_connect('postgresql://test');finance.connect_database()
   with tenancy.company_scope(self.seed['companies']['b']):
    second=tenancy.schema_name();tenancy.scoped_connect('postgresql://test')
   self.assertNotEqual(first,second)
   queries=[call.args[0].as_string() for call in cursor.execute.call_args_list if hasattr(call.args[0],'as_string')]
   self.assertTrue(any('SET search_path TO "'+first+'"'==q for q in queries))
   self.assertTrue(any('SET search_path TO "'+second+'"'==q for q in queries))
   self.assertTrue(all('public' not in q for q in queries))
 def test_concurrent_request_scopes(self):
  from concurrent.futures import ThreadPoolExecutor
  def inspect(company_id):
   with tenancy.company_scope(self.seed['companies'][company_id]):
    import time;time.sleep(.01)
    return list(module.VEHICLES)[0]['id'],str(module.COMPANY_NAME)
  with ThreadPoolExecutor(max_workers=2) as pool:
   results=list(pool.map(inspect,['a','b']*10))
  self.assertEqual(results,[('va','Alpha'),('vb','Beta')]*10)
  self.assertIsNone(tenancy.company())
 def test_gps_list_import(self):
  token=self.csrf(self.a)
  with patch('app.gps_list_vehicles',return_value=[{'id':'remote-a','name':'Remote A','plate':'REMOTE'}]):
   self.assertEqual(self.a.post('/company/gps/settings',data={'provider':'navirec','account_id':'account-a','navirec_token':'private-token','csrf':token}).status_code,200)
   self.assertEqual(self.a.post('/company/gps/settings',data={'action':'import','vehicles':['remote-a'],'csrf':token}).status_code,302)
   self.assertEqual(self.a.post('/company/gps/settings',data={'action':'import','vehicles':['remote-a'],'csrf':token}).status_code,302)
  with module.TENANT_ACCOUNTS_LOCK:data=module._load_tenant_accounts()
  self.assertEqual(sum(v.get('navirec_id')=='remote-a' for v in data['companies']['a']['vehicles']),1)
  self.assertNotIn('REMOTE',self.b.get('/vehicles').get_data(as_text=True))
 def test_adapter_navirec_and_wialon(self):
  response=Mock(status_code=200);response.json.return_value=[{'id':'nav-a','name':'Nav A','registration':'AA1'}]
  with patch('gps_providers.requests.get',return_value=response) as get:
   self.assertEqual(gps_providers.list_vehicles({'provider':'navirec','token':'token-a','account_id':'account-a'})[0]['id'],'nav-a')
   self.assertEqual(get.call_args.kwargs['params']['account'],'account-a')
  login=Mock(status_code=200);login.json.return_value={'eid':'temporary-session'}
  units=Mock(status_code=200);units.json.return_value={'items':[{'id':12,'nm':'Van','pos':{'y':52,'x':19,'t':1760000000,'s':10}}]}
  logout=Mock(status_code=200);logout.json.return_value={}
  with patch('gps_providers.requests.post',side_effect=[login,units,logout]) as post:
   positions=gps_providers.live_states({'provider':'wialon','token':'token-b'},[])
   self.assertEqual(positions[0]['location']['latitude'],52)
   self.assertEqual(post.call_args.kwargs['data']['svc'],'core/logout')

if __name__=='__main__': unittest.main(verbosity=2)
