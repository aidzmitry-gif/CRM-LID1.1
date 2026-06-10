"""HTTP-API модуля Leads. Монтируется ядром под префиксом ``/leads``.

Перенесено из репозитория CRM (бывшие эндпоинты ``/sales/leads*``). Главное
архитектурное отличие после выноса: конвертация не создаёт сделку напрямую —
модуль публикует ``leads.lead.converted``, сделку создаёт модуль sales (§2.4/§2.5).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Counterparty
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.leads.ai import qualify_lead
from modules.leads.leads import lead_priority, route_lead, score_lead
from modules.leads.models import Lead
from modules.leads.schemas import (
    LeadConvertOut,
    LeadCreate,
    LeadOut,
    LeadQualifyOut,
    LeadRouteOut,
)

router = APIRouter(tags=["leads"])


async def _known_customer(session: AsyncSession, company: str) -> bool:
    """Лид от действующего контрагента? (повышает балл, даёт воронку «постоянные»)."""
    if not company:
        return False
    cp = (
        await session.execute(select(Counterparty).where(Counterparty.name == company))
    ).scalars().first()
    return cp is not None


async def _manager_loads(session: AsyncSession) -> dict[str, int]:
    """Загрузка менеджеров для роутинга: активные распределённые лиды.

    Открытые сделки менеджеров живут в модуле sales; после выноса лидов их вклад
    в загрузку добавится через фасад ядра или проекцию (без импорта модулей, §2.4).
    """
    rows = (
        await session.execute(
            select(Lead.assigned_to, func.count())
            .where(Lead.status == "routed", Lead.assigned_to != "")
            .group_by(Lead.assigned_to)
        )
    ).all()
    return {name: n for name, n in rows}


@router.get("/ping")
async def ping() -> dict:
    """Проверка, что модуль смонтирован."""
    return {"module": "leads", "status": "ok"}


@router.get("", response_model=list[LeadOut])
async def list_leads(status: str = "", session: AsyncSession = Depends(get_session)):
    """Приём лидов: входящие заявки воронки (новые — первыми; опц. фильтр по статусу)."""
    query = select(Lead).order_by(Lead.id.desc())
    if status:
        query = query.where(Lead.status == status)
    return (await session.execute(query)).scalars().all()


@router.post("", response_model=LeadOut, status_code=201)
async def create_lead(
    payload: LeadCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Принять лид из канала (сайт/мессенджер/e-mail/телефония/тендер) → событие в шину."""
    lead = Lead(**payload.model_dump())
    session.add(lead)
    await session.flush()
    core.event_bus.emit(
        session,
        "leads.lead.received",
        {"lead_id": lead.id, "source": lead.source, "entity_ref": f"lead:{lead.id}"},
    )
    await session.commit()
    return lead


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: int, session: AsyncSession = Depends(get_session)):
    """Один лид по id."""
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    return lead


@router.post("/{lead_id}/qualify", response_model=LeadQualifyOut)
async def qualify(
    lead_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Квалифицировать лид (Lead Qualifier): балл + вердикт целевой/нецелевой.

    Скоринг детерминирован и работает без AI. При включённом AI-слое добавляется
    текстовое обоснование через общий шлюз, действие фиксируется ``ai.lead.qualified``
    (→ audit, §3.3); без AI — событие ``leads.lead.qualified``. Под-фича за
    feature-flag, без переписывания механики (§2.5).
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")

    known = await _known_customer(session, lead.company)
    score, verdict, reason = score_lead(lead, known)
    lead.score = score
    lead.qualification = verdict
    lead.reason = reason
    if lead.status == "new":
        lead.status = "qualified"

    rationale: str | None = None
    model: str | None = None
    llm = core.services.llm
    if llm.enabled:
        rationale = await qualify_lead(llm, lead, score, verdict)
        model = llm.model or "mock"
        core.event_bus.emit(
            session,
            "ai.lead.qualified",
            {
                "lead_id": lead.id, "score": score, "verdict": verdict, "model": model,
                "actor": "AI", "entity_ref": f"lead:{lead.id}",
            },
        )
    else:
        core.event_bus.emit(
            session,
            "leads.lead.qualified",
            {"lead_id": lead.id, "score": score, "verdict": verdict, "entity_ref": f"lead:{lead.id}"},
        )
    await session.commit()
    return LeadQualifyOut(
        id=lead.id, status=lead.status, score=score, qualification=verdict,
        reason=reason, ai_rationale=rationale, model=model,
    )


@router.post("/{lead_id}/route", response_model=LeadRouteOut)
async def route(
    lead_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Распределить лид на менеджера по правилам (география/продукт/нагрузка/воронка).

    Публикует ``leads.lead.routed`` (→ audit). Уже сконвертированный лид — 409.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status == "converted":
        raise HTTPException(status_code=409, detail="Лид уже сконвертирован в сделку")

    known = await _known_customer(session, lead.company)
    loads = await _manager_loads(session)
    manager, funnel = route_lead(lead, loads, known)
    lead.assigned_to = manager
    lead.funnel = funnel
    lead.status = "routed"
    core.event_bus.emit(
        session,
        "leads.lead.routed",
        {
            "lead_id": lead.id, "assigned_to": manager, "funnel": funnel,
            "entity_ref": f"lead:{lead.id}",
        },
    )
    await session.commit()
    return LeadRouteOut(id=lead.id, status=lead.status, assigned_to=manager, funnel=funnel)


@router.post("/{lead_id}/convert", response_model=LeadConvertOut, status_code=201)
async def convert_lead(
    lead_id: int,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Конвертировать распределённый лид — публикует ``leads.lead.converted``.

    Сделку создаёт модуль sales (репозиторий CRM): он подписан на это событие,
    создаёт ``Deal`` (стадия ``new``, ответственный = назначенный менеджер,
    приоритет по баллу) и отвечает ``sales.deal.created`` с ``lead_id``/``deal_id`` —
    обработчик ``events.on_deal_created_from_lead`` проставит лиду ссылку.
    Требует предварительного распределения (иначе 409).
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status == "converted":
        raise HTTPException(status_code=409, detail="Лид уже сконвертирован в сделку")
    if lead.status != "routed":
        raise HTTPException(status_code=409, detail="Сначала распределите лид на менеджера")

    lead.status = "converted"
    core.event_bus.emit(
        session,
        "leads.lead.converted",
        {
            "lead_id": lead.id,
            "title": lead.product or (lead.message[:60] if lead.message else "") or "Лид",
            "counterparty": lead.company or lead.name or "Новый лид",
            "owner": lead.assigned_to,
            "priority": lead_priority(lead.score),
            "entity_ref": f"lead:{lead.id}",
        },
    )
    await session.commit()
    return LeadConvertOut(lead_id=lead.id, status=lead.status)
