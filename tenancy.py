"""Request-local company data and isolated PostgreSQL namespaces.

No process globals are swapped: concurrent requests resolve independent proxies.
The legacy company's public tables and original files remain unchanged.
"""
import contextvars
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from flask import current_app, has_app_context

_scope = contextvars.ContextVar('tranviq_company_scope', default=None)
_caches = {}
_cache_lock = threading.Lock()

def company():
    return _scope.get()

def bind_company(value):
    return _scope.set(value)

def clear_company(token):
    _scope.reset(token)

@contextmanager
def company_scope(value):
    token = bind_company(value)
    try: yield
    finally: clear_company(token)

def schema_name():
    value = company()
    return 'tenant_' + hashlib.sha256(str(value['id']).encode()).hexdigest()[:32] if value else 'public'

def scoped_connect(database_url, **kwargs):
    import psycopg
    from psycopg import sql
    connection = psycopg.connect(database_url, **kwargs)
    if company():
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', (int(hashlib.sha256(schema_name().encode()).hexdigest()[:15], 16),))
                cursor.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema_name())))
                # Never include public in tenant search_path: missing tables must fail closed.
                cursor.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema_name())))
            connection.commit()
        except Exception:
            connection.close()
            raise
    return connection

def cache(name):
    key = ((company() or {}).get('id', '__legacy__'), name)
    with _cache_lock:
        return _caches.setdefault(key, {})

def json_read(name, default):
    value = company()
    if not value: raise RuntimeError('No company selected')
    with current_app.config['TENANT_STORE_LOCK']:
        data = current_app.config['TENANT_STORE_READ']()
        return data['companies'][value['id']].get('state', {}).get(name, default)

def json_write(name, value):
    selected = company()
    if not selected: raise RuntimeError('No company selected')
    with current_app.config['TENANT_STORE_LOCK']:
        data = current_app.config['TENANT_STORE_READ']()
        data['companies'][selected['id']].setdefault('state', {})[name] = value
        current_app.config['TENANT_STORE_WRITE'](data)

def file_path(original):
    value = company()
    if not value: return original
    parent = os.path.dirname(original)
    return os.path.join(parent, 'companies', hashlib.sha256(value['id'].encode()).hexdigest(), os.path.basename(original))

def provider_vehicle_id(vehicle_id):
    selected = company()
    if not selected: return vehicle_id
    vehicle = next((v for v in selected.get('vehicles', []) if str(v['id']) == str(vehicle_id)), None)
    return (vehicle.get('navirec_id') or vehicle['id']) if vehicle else ''

def local_vehicle_id(provider_id):
    selected = company()
    if not selected: return provider_id
    provider_id = str(provider_id or '').rstrip('/').split('/')[-1]
    vehicle = next((v for v in selected.get('vehicles', []) if str(v.get('navirec_id') or v['id']) == provider_id and v.get('gps_provider', selected.get('gps', {}).get('provider', 'navirec')) == selected.get('gps', {}).get('provider', 'navirec')), None)
    return vehicle['id'] if vehicle else ''

def normalize_states(items):
    if not company(): return items
    result = []
    for item in items:
        if not isinstance(item, dict): continue
        item = dict(item)
        external = item.get('vehicle') or item.get('vehicle_id') or item.get('vehicle_url')
        local = local_vehicle_id(external)
        if external and not local: continue
        if local:
            item['vehicle'] = local
            item['vehicle_id'] = local
        result.append(item)
    return result
