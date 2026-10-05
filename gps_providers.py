"""Explicit GPS adapters. Unsupported capabilities return unknown, never invented data."""
import json
from datetime import datetime, timezone
import requests

PROVIDERS = {'navirec': 'Navirec', 'wialon': 'Wialon Hosting', 'manual': 'Без GPS / ручна позиція', 'other': 'Інший GPS (потрібне підключення)'}
class GPSError(RuntimeError): pass

def _items(payload):
    return payload if isinstance(payload, list) else payload.get('results', [])

def _navirec(settings, endpoint):
    if not settings.get('token') or not settings.get('account_id'):
        raise GPSError('Вкажіть токен та ID акаунта Navirec.')
    result = requests.get('https://api.navirec.com/'+endpoint+'/', headers={'Authorization':'Token '+settings['token'], 'Accept':'application/json; version=1.52.1'}, params={'account':settings['account_id'], 'page_size':500}, timeout=20, allow_redirects=False)
    if result.status_code != 200: raise GPSError('Navirec не надав дані. Перевірте токен і доступ до акаунта.')
    data = result.json()
    if isinstance(data, dict) and data.get('next'):
        raise GPSError('Акаунт містить більше 500 записів. Потрібне додаткове налаштування імпорту.')
    return _items(data)

def _wialon(settings):
    token = settings.get('token')
    if not token: raise GPSError('Вкажіть API-токен Wialon.')
    # Only official Hosting hosts; a configurable arbitrary URL could leak credentials.
    host = {region:'https://hst-api.wialon.'+region+'/wialon/ajax.html' for region in ('com','eu','us','org')}.get(settings.get('region','com'))
    if not host: raise GPSError('Оберіть регіон Wialon.')
    def call(service, params, sid=None):
        data={'svc':service,'params':json.dumps(params)}
        if sid: data['sid']=sid
        response=requests.post(host, data=data, timeout=20, allow_redirects=False)
        if response.status_code != 200: raise GPSError('Wialon тимчасово недоступний.')
        result=response.json()
        if not isinstance(result, dict) or result.get('error'): raise GPSError('Wialon відхилив запит. Перевірте токен і права доступу.')
        return result
    login=call('token/login',{'token':token})
    sid=login.get('eid')
    if not sid: raise GPSError('Не вдалося відкрити сесію Wialon.')
    try:
        result=call('core/search_items', {'spec':{'itemsType':'avl_unit','propName':'sys_name','propValueMask':'*','sortType':'sys_name'},'force':1,'flags':1025,'from':0,'to':0},sid)
        return result.get('items',[])
    finally:
        try: call('core/logout',{},sid)
        except (GPSError,requests.RequestException,ValueError): pass

def list_vehicles(settings):
    provider=settings.get('provider','navirec')
    try:
        if provider=='navirec':
            return [{'id':str(v.get('id') or v.get('url') or '').rstrip('/').split('/')[-1], 'name':str(v.get('name') or v.get('registration') or 'Автомобіль'), 'plate':str(v.get('registration') or v.get('plate') or v.get('name') or '')} for v in _navirec(settings,'vehicles')]
        if provider=='wialon':return [{'id':str(v['id']),'name':str(v.get('nm') or v['id']),'plate':str(v.get('nm') or v['id'])} for v in _wialon(settings)]
        if provider=='manual':return []
        raise GPSError('Цей постачальник ще не підключений.')
    except (requests.RequestException,ValueError,TypeError,KeyError):raise GPSError('Не вдалося отримати список автомобілів. Спробуйте ще раз.') from None

def live_states(settings, vehicles):
    provider=settings.get('provider','navirec')
    try:
        if provider=='navirec':return _navirec(settings,'last_vehicle_states')
        if provider=='wialon':
            result=[]
            for v in _wialon(settings):
                pos=v.get('pos') or {}
                if pos.get('y') is None or pos.get('x') is None:continue
                result.append({'vehicle':str(v['id']), 'location':{'latitude':pos['y'],'longitude':pos['x']}, 'speed':pos.get('s'), 'time':datetime.fromtimestamp(pos['t'],timezone.utc).isoformat() if pos.get('t') else None, 'activity':'moving' if (pos.get('s') or 0)>2 else 'parked', 'gps_source':'Wialon'})
            return result
        if provider=='manual':return [{'vehicle':v['id'],'location':{'latitude':v['latitude'],'longitude':v['longitude']},'time':v.get('position_at'),'gps_source':'manual'} for v in vehicles if v.get('latitude') is not None and v.get('longitude') is not None]
        raise GPSError('Цей постачальник ще не підключений.')
    except (requests.RequestException,ValueError,TypeError,KeyError):raise GPSError('Не вдалося отримати позиції GPS.') from None
