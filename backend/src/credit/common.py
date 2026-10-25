from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, details: list | None = None):
        super().__init__(message)
        self.status_code = self.status = status_code
        self.message = message
        self.details = details or []


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def new_id():
    return str(uuid4())


def require_role(actor, *roles):
    if actor.get('role') not in roles:
        raise ApiError(403, 'Действие не разрешено для роли пользователя.')


async def require_company(store, actor, company_id, owner=False):
    require_role(actor, 'client')
    company = await store.get('companies', company_id)
    memberships = await store.list('company_members', user_id=actor['id'], company_id=str(company_id))
    if company is None or not memberships:
        raise ApiError(404, 'Компания не найдена.')
    if owner and memberships[0]['role'] != 'owner':
        raise ApiError(403, 'Изменять компанию может только собственник.')
    return company


async def require_application(store, actor, application_id, bank=False, lock=False):
    require_role(actor, 'bank_employee' if bank else 'client')
    # Check visibility before taking a row lock. A client who knows a foreign
    # application ID must not be able to lock that company's workflow row.
    app = await store.get('applications', application_id)
    if app is None or (bank and app['status'] == 'Draft'):
        raise ApiError(404, 'Заявка не найдена.')
    if not bank:
        await require_company(store, actor, app['company_id'])
    if lock:
        app = await store.get('applications', application_id, lock=True)
        if app is None or (bank and app['status'] == 'Draft'):
            raise ApiError(404, 'Заявка не найдена.')
    return app


def check_version(entity, expected):
    if entity['version'] != expected:
        raise ApiError(409, 'Данные изменились. Загрузите актуальную версию.')


async def touch(store, app):
    app['version'] += 1
    app['updated_at'] = now()
    await store.save('applications', app)
    return app


async def audit(store, actor, action, object_type, object_id, result='success', **extra):
    item = {'id': new_id(), 'actor_type': (actor or {}).get('role', 'system'), 'action': action,
            'object_type': object_type, 'object_id': str(object_id), 'result': result, 'created_at': now()}
    if actor and actor.get('id'):
        item['actor_id'] = actor['id']
    item.update(extra)
    await store.insert('audit_events', item)


async def event(store, app, actor, type, message):
    item = {'id': new_id(), 'application_id': app['id'], 'type': type,
            'status': app['status'], 'message': message,
            'actor_type': (actor or {}).get('role', 'system'), 'created_at': now()}
    await store.insert('application_events', item)
    await audit(store, actor, type, 'application', app['id'], version=app['version'], snapshot_id=app.get('current_snapshot_id'))
    return item


def application_view(app, bank=False):
    fields = ('id', 'company_id', 'status', 'version', 'data', 'current_snapshot_id', 'decision', 'created_at', 'updated_at')
    result = {key: app[key] for key in fields if key in app}
    actions = {
        'Draft': ['edit', 'upload_document', 'delete_document', 'submit'],
        'NeedsInfo': ['upload_document', 'delete_document', 'answer_information_request'],
        'Offered': ['accept_offer', 'decline_offer'], 'Accepted': ['sign'],
    }
    result['available_actions'] = [] if bank else actions.get(app['status'], []).copy()
    if bank:
        result['checks'] = app.get('checks', [])
        result['decisions'] = app.get('decisions', [])
        for key in ('processing', 'scoring_result'):
            if key in app:
                result[key] = app[key]
        bank_actions = ['request_information', 'approve', 'reject'] if app['status'] == 'ManualReview' else []
        if app.get('processing', {}).get('retry_available'):
            bank_actions.append('retry_processing')
        result['available_bank_actions'] = bank_actions
    return result


def page(items, query=None, *, limit=None, offset=None):
    query = query or {}
    limit = int(query.get('limit', 20) if limit is None else limit)
    offset = int(query.get('offset', 0) if offset is None else offset)
    ordered = sorted(items, key=lambda row: (row.get('created_at', ''), row['id']), reverse=True)
    return {'items': ordered[offset:offset+limit], 'limit': limit, 'offset': offset, 'total': len(items)}


async def enqueue(store, app, stage, source):
    created = now()
    job = {'id': new_id(), 'application_id': app['id'], 'snapshot_id': app['current_snapshot_id'],
           'stage': stage, 'source': source, 'status': 'queued', 'attempt': 0, 'cycle_attempt': 0,
           'run_at': created, 'request_id': new_id(), 'created_at': created}
    await store.insert('jobs', job)
    app['processing'] = {'stage': stage, 'status': 'queued', 'attempt': 0, 'retry_available': False}
    return job
