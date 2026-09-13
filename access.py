"""Fail-closed lead endpoints and numeric owner SQL scope."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from core.services.crm_access import CrmAccess, get_crm_access
from modules.leads.models import Lead


def scope_leads(stmt, access):
    return stmt.where(Lead.owner_id == access.employee_id) if access.own_only else stmt


async def visible_lead(session, lead_id, access, *, lock=False):
    stmt = scope_leads(select(Lead).where(Lead.id == lead_id), access)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    lead = (await session.execute(stmt)).scalar_one_or_none()
    if lead is None:
        raise HTTPException(404, "Лид не найден")
    return lead


OWN_ROUTES = frozenset({'ping', 'list_leads', 'create_lead', 'get_lead', 'qualify',
                        'route', 'convert_lead', 'list_items', 'replace_items'})


async def deny_unscoped_own_routes(request: Request, access: CrmAccess = Depends(get_crm_access)):
    if access.own_only and request.scope.get('route').name not in OWN_ROUTES:
        raise HTTPException(403, "Действие недоступно при личной видимости лидов")
