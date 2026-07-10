"""Обработчики событий модуля Leads."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("aios.leads")


async def on_campaign_launched(payload: dict, ctx) -> None:
    """Кампания запущена → привлечённые лиды попадают в приём лидов (marketing → leads).

    Маркетинг питает воронку через её вход: создаёт записи лидов (front-of-funnel),
    которые менеджер/AI затем квалифицирует, распределяет и превращает в сделки —
    а не создаёт сделки напрямую. Так замыкается цикл «кампания → лиды → воронка».
    """
    if ctx is None:
        return
    from modules.leads.leads import LEAD_SOURCES, apply_initial_score
    from modules.leads.models import Lead

    count = min(int(payload.get("leads", 0) or 0), 10)
    name = payload.get("name", "Кампания")
    channel = payload.get("channel", "site")
    source = channel if channel in LEAD_SOURCES else "site"
    # UTM кампании (Цикл 4) — на каждый заведённый лид: отчёт качества источников
    # (routes.py) и marketing-атрибуция (leads.lead.received) видят, откуда лид пришёл.
    utm_source = str(payload.get("utm_source") or "").strip()
    utm_medium = str(payload.get("utm_medium") or "").strip()
    utm_campaign = str(payload.get("utm_campaign") or "").strip()
    for _ in range(count):
        lead = Lead(
            source=source,
            message=f"Заявка из кампании «{name}» (канал {channel})",
            status="new",
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
        )
        ctx.session.add(lead)
        await apply_initial_score(lead, ctx.session)  # балл сразу, статус остаётся new
        await ctx.session.flush()
        ctx.services.event_bus.emit(
            ctx.session,
            "leads.lead.received",
            {
                "lead_id": lead.id,
                "source": source,
                "entity_ref": f"lead:{lead.id}",
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
            },
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

    from sqlalchemy import select

    from core.domain.models import Contact
    from modules.leads.leads import apply_initial_score, find_open_lead_by_phone, phone_tail
    from modules.leads.models import Lead

    tail = phone_tail(phone)
    if tail:
        known = (
            await ctx.session.execute(
                select(Contact).where(Contact.phone.isnot(None), Contact.phone.like(f"%{tail}"))
            )
        ).scalars().first()
        if known is not None:
            return  # известный контакт → обрабатывает sales (сделка/продавец)
    dup = await find_open_lead_by_phone(ctx.session, phone)
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
    await apply_initial_score(lead, ctx.session)
    await ctx.session.flush()
    ctx.services.event_bus.emit(
        ctx.session,
        "leads.lead.received",
        {"lead_id": lead.id, "source": "phone", "entity_ref": f"lead:{lead.id}"},
    )
    logger.info("Leads: входящий звонок с %s → лид %s", phone, lead.id)


async def on_intake_lead(payload: dict, ctx) -> None:
    """Веб-форма/почта (integrations → leads): заявка становится лидом.

    Публичные коннекторы (``/integrations/web/lead``, ``/integrations/email/inbound``)
    сами лид не создают — публикуют ``intake.lead.received`` (модули не видят друг
    друга напрямую, §2.4). Здесь заявка превращается в ``Lead`` со статусом ``new``
    и лид входит в общую воронку приёма тем же событием, что и остальные каналы.

    Дедуп: открытый лид с тем же телефоном/e-mail — новый лид не создаём, а
    дописываем обращение к существующему (без нового события: он и так на виду).
    """
    if ctx is None:
        return
    from modules.leads.leads import (
        LEAD_SOURCES,
        apply_initial_score,
        find_open_lead_by_email,
        find_open_lead_by_phone,
    )
    from modules.leads.models import Lead

    source = payload.get("source") or "site"
    if source not in LEAD_SOURCES:
        source = "site"
    phone = (payload.get("phone") or "").strip() or None
    email = (payload.get("email") or "").strip() or None
    message = (payload.get("message") or "").strip()
    # UTM из _extract_utm (modules/integrations/routes.py) — те же ключи, аддитивно.
    utm_source = (payload.get("utm_source") or "").strip()
    utm_medium = (payload.get("utm_medium") or "").strip()
    utm_campaign = (payload.get("utm_campaign") or "").strip()
    landing_url = (payload.get("landing_url") or "").strip()

    dup = await find_open_lead_by_phone(ctx.session, phone)
    if dup is None:
        dup = await find_open_lead_by_email(ctx.session, email)
    if dup is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        note = f"Повторное обращение ({source}, {stamp}): {message}".strip()
        dup.message = f"{dup.message}\n---\n{note}" if dup.message else note
        await apply_initial_score(dup, ctx.session)  # пересчёт балла с учётом нового обращения
        logger.info("Leads: повторное обращение (%s) → дописано к лиду %s", source, dup.id)
        return

    lead = Lead(
        source=source,
        name=(payload.get("name") or "").strip(),
        company=(payload.get("company") or "").strip(),
        phone=phone,
        email=email,
        region=(payload.get("region") or "").strip(),
        product=(payload.get("product") or "").strip(),
        message=message,
        status="new",
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
    )
    ctx.session.add(lead)
    await apply_initial_score(lead, ctx.session)
    await ctx.session.flush()
    ctx.services.event_bus.emit(
        ctx.session,
        "leads.lead.received",
        {
            "lead_id": lead.id,
            "source": source,
            "entity_ref": f"lead:{lead.id}",
            "utm_source": utm_source,
            "utm_medium": utm_medium,
            "utm_campaign": utm_campaign,
            "landing_url": landing_url,
        },
    )
    logger.info("Leads: интейк (%s) → лид %s", source, lead.id)


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
