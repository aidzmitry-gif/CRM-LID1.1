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
