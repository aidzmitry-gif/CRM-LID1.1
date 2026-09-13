"""Explicit CRM-linked lead intake. No legacy MDM, revival or wake operations."""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from core.services.crm_owners import resolve_crm_owner
from modules.leads.leads import is_key_lead, score_lead
from modules.leads.models import Lead


async def resolve_link(core, session, lead, access, *, lock=True):
    gateway = core.services.crm_clients
    if gateway is None:
        raise HTTPException(503, "Связь с CRM-клиентами временно недоступна")
    return await gateway.resolve_link(session, actor=access, client_id=lead.crm_client_id,
                                      contact_id=lead.crm_contact_id,
                                      expected_owner_id=lead.owner_id, lock=lock)


async def create_linked_lead(payload, core, session, access):
    if payload.crm_client_id is None or payload.request_key is None:
        raise HTTPException(422, "Выберите CRM-клиента и передайте ключ повтора")
    owner_id = access.employee_id if access.own_only else payload.owner_id
    if access.own_only and payload.owner_id not in (None, owner_id):
        raise HTTPException(403, "Нельзя назначить чужого владельца лида")
    owner = await resolve_crm_owner(session, employee_id=owner_id, lock=True)
    values = payload.model_dump()
    values['owner_id'] = owner.employee_id
    request_hash = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = (await session.execute(select(Lead).where(
        Lead.owner_id == owner.employee_id, Lead.request_key == payload.request_key))).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Ключ повтора уже использован для другого лида")
        await resolve_link(core, session, existing, access)
        existing.is_key = is_key_lead(existing)
        return existing
    lead = Lead(**values, request_hash=request_hash)
    link = await resolve_link(core, session, lead, access)
    # Client row lock serializes same-client retries and duplicate checks.
    existing = (await session.execute(select(Lead).where(
        Lead.owner_id == owner.employee_id, Lead.request_key == payload.request_key))).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Ключ повтора уже использован для другого лида")
        existing.is_key = is_key_lead(existing)
        return existing
    if link.email is not None and len(link.email) > 128:
        raise HTTPException(422, "Email контакта превышает лимит лида: 128 символов")
    lead.company = link.client_name
    lead.name = link.contact_name or payload.name
    lead.phone = link.phone if link.contact_id is not None else payload.phone
    lead.email = link.email if link.contact_id is not None else payload.email
    tests = []
    if lead.phone:
        tests.append(Lead.phone == lead.phone)
    if lead.email:
        tests.append(Lead.email == lead.email)
    if tests:
        duplicate = (await session.execute(select(Lead).where(
            Lead.owner_id == owner.employee_id, Lead.crm_client_id == link.client_id,
            Lead.status.in_(['new', 'qualified', 'routed']), or_(*tests)))).scalars().first()
        if duplicate:
            raise HTTPException(409, {'duplicate_of': duplicate.id, 'message': 'Открытый лид уже существует'})
    lead.score, lead.qualification, lead.reason = score_lead(lead, True)
    if session.get_bind().dialect.name == "sqlite":
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        if not raw.driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    try:
        async with session.begin_nested():
            session.add(lead)
            await session.flush()
            core.event_bus.emit(session, 'leads.lead.received', {
                'lead_id': lead.id, 'source': lead.source, 'entity_ref': f'lead:{lead.id}'})
    except IntegrityError:
        existing = (await session.execute(select(Lead).where(
            Lead.owner_id == owner.employee_id, Lead.request_key == payload.request_key))).scalar_one_or_none()
        if existing is None:
            raise
        if existing.request_hash != request_hash:
            raise HTTPException(409, "Ключ повтора уже использован для другого лида") from None
        existing.is_key = is_key_lead(existing)
        return existing
    await session.commit()
    await session.refresh(lead)
    lead.is_key = is_key_lead(lead)
    return lead
