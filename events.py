"""Обработчики событий модуля Leads."""
from __future__ import annotations

import logging

logger = logging.getLogger("aios.leads")


async def on_campaign_launched(payload: dict, ctx) -> None:
    """Кампания запущена → привлечённые лиды попадают в приём лидов (marketing → leads).

    Маркетинг питает воронку через её вход: создаёт записи лидов (front-of-funnel),
    которые менеджер/AI затем квалифицирует, распределяет и превращает в сделки —
    а не создаёт сделки напрямую. Так замыкается цикл «кампания → лиды → воронка».
    """
    if ctx is None:
        return
    from modules.leads.leads import LEAD_SOURCES
    from modules.leads.models import Lead

    count = min(int(payload.get("leads", 0) or 0), 10)
    name = payload.get("name", "Кампания")
    channel = payload.get("channel", "site")
    source = channel if channel in LEAD_SOURCES else "site"
    for _ in range(count):
        ctx.session.add(
            Lead(
                source=source,
                message=f"Заявка из кампании «{name}» (канал {channel})",
                status="new",
            )
        )
    logger.info("Leads: из кампании «%s» принято лидов: %d", name, count)


async def on_call_logged(payload: dict, ctx) -> None:
    """Входящий звонок с НЕИЗВЕСТНОГО номера → новый лид (sales → leads).

    Телефония sales журналирует звонок и публикует ``sales.call.logged`` (несёт
    ``agent_ext`` — кто поднял трубку). Известный звонящий (есть контакт в shared
    kernel) обрабатывается продажами (резолв продавца/сделки, §2.4); неизвестный —
    заводится лидом источника ``phone`` здесь, чтобы ни одно обращение не потерялось
    (правило «обращение → запись + маршрутизация»). Дедуп: пока есть открытый лид с
    этим номером, повторные звонки дубль не плодят.
    """
    if ctx is None or payload.get("direction") != "in":
        return
    phone = (payload.get("phone") or "").strip()
    if not phone:
        return

    import re

    from sqlalchemy import select

    from core.domain.models import Contact
    from modules.leads.models import Lead

    tail = re.sub(r"\D", "", phone)[-9:]  # значащий хвост: контакты могут быть без кода страны
    if tail:
        # ponytail: LIKE-скан по хвосту — при росте базы нормализованная колонка + индекс
        known = (
            await ctx.session.execute(
                select(Contact).where(Contact.phone.isnot(None), Contact.phone.like(f"%{tail}"))
            )
        ).scalars().first()
        if known is not None:
            return  # известный контакт → обрабатывает sales (сделка/продавец)
        dup = (
            await ctx.session.execute(
                select(Lead).where(
                    Lead.phone.like(f"%{tail}"),
                    Lead.status.in_(("new", "qualified", "routed")),  # только ОТКРЫТЫЕ
                )
            )
        ).scalars().first()
        if dup is not None:
            return  # уже есть открытый лид с этого номера (терминальные converted/rejected — не помеха)

    agent = payload.get("agent_ext") or ""
    lead = Lead(
        source="phone",
        phone=phone,
        message=f"Входящий звонок (доб. {agent})" if agent else "Входящий звонок",
        status="new",
    )
    ctx.session.add(lead)
    await ctx.session.flush()
    ctx.services.event_bus.emit(
        ctx.session,
        "leads.lead.received",
        {"lead_id": lead.id, "source": "phone", "entity_ref": f"lead:{lead.id}"},
    )
    logger.info("Leads: входящий звонок с %s → лид %s", phone, lead.id)


async def on_deal_created_from_lead(payload: dict, ctx) -> None:
    """Сделка создана из лида (sales → leads): проставить лиду ссылку на сделку.

    Модуль sales (репозиторий CRM), создав сделку по событию ``leads.lead.converted``,
    отвечает ``sales.deal.created`` с ``lead_id``/``deal_id`` — здесь замыкается
    обратная связь: лид получает ``deal_id`` без импорта модулей друг другом (§2.4).
    """
    if ctx is None:
        return
    lead_id = payload.get("lead_id")
    deal_id = payload.get("deal_id")
    if not lead_id or not deal_id:
        return
    from modules.leads.models import Lead

    lead = await ctx.session.get(Lead, lead_id)
    if lead is not None:
        lead.deal_id = deal_id
        logger.info("Leads: лид %s связан со сделкой %s", lead_id, deal_id)
