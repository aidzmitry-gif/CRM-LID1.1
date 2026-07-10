"""Lead Qualifier & Router — квалификация и распределение лидов (ФАЗА 1).

Детерминированный движок без модели: скоринг лида по эвристикам (целевой/нет) и
правила распределения на менеджера (география, продукт, нагрузка, тип воронки).
AI-подмодуль (``ai.qualify_lead``) добавляет текстовое обоснование поверх — за
feature-flag, не подменяя этот балл и не переписывая механику (§2.5). Это первый
AI-пилот дорожной карты (Lead Qualifier & Router).
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Counterparty
from modules.leads.models import Lead

# Источники лида (каналы приёма): сайт/лендинг, мессенджеры, e-mail, телефония, тендеры.
LEAD_SOURCES = ("site", "telegram", "whatsapp", "email", "phone", "tender")

# Открытые статусы — дедуп и загрузка менеджеров смотрят только на них
# (терминальные converted/rejected не мешают новому обращению/лиду).
OPEN_STATUSES = ("new", "qualified", "routed")

# Менеджеры и их специализация — для маршрутизации по географии/продукту.
# Последний (без регионов/продуктов) — универсал: catch-all при отсутствии совпадений.
MANAGERS: list[dict] = [
    {
        "name": "Иванов И.И.",
        "regions": ["минск", "минская"],
        "products": ["металл", "прокат", "арматура", "лист"],
    },
    {
        "name": "Петров П.П.",
        "regions": ["гомель", "гомельская", "могилёв", "могилёвская"],
        "products": ["оборудование", "станок", "линия", "комплектующие"],
    },
    {"name": "Сидоров С.С.", "regions": [], "products": []},
]

# Порог скоринга: балл >= порога → целевой лид.
QUALIFY_THRESHOLD = 50

# Причины отказа (Слайс 4) — фиксированный список вместо свободного текста,
# чтобы отчёты по причинам отказа были сравнимы (не «дубль»/«Дубль»/«дублирует»).
REJECT_REASONS = ("не наш профиль", "нет бюджета", "дубль", "конкурент")


def score_lead(lead: Lead, known_customer: bool = False) -> tuple[int, str, str]:
    """Оценить лида эвристиками → (балл 0..100, вердикт ``target|non-target``, причина).

    Без модели и детерминированно: по заполненности профиля и качеству канала.
    AI добавляет обоснование поверх (``ai.qualify_lead``), не подменяя этот балл.
    """
    score = 0
    reasons: list[str] = []

    if lead.phone:
        score += 20
        reasons.append("есть телефон")
    if lead.email:
        score += 15
        reasons.append("есть e-mail")
    company = (lead.company or "").strip()
    if company and company != "Новый лид":
        score += 15
        reasons.append("указана компания")
    if known_customer:
        score += 15
        reasons.append("действующий контрагент")
    if lead.product:
        score += 15
        reasons.append("указан продукт")
    if lead.message and len(lead.message) >= 20:
        score += 10
        reasons.append("развёрнутый запрос")

    # качество канала приёма
    channel_bonus = {"tender": 15, "site": 10, "email": 8, "whatsapp": 5, "telegram": 5, "phone": 3}
    if channel_bonus.get(lead.source):
        score += channel_bonus[lead.source]

    score = min(100, score)
    verdict = "target" if score >= QUALIFY_THRESHOLD else "non-target"
    reason = ", ".join(reasons) or "недостаточно данных"
    return score, verdict, reason


def choose_funnel(lead: Lead, known_customer: bool) -> str:
    """Тип воронки по лиду: тендер / проект / постоянный клиент / новый."""
    if lead.source == "tender":
        return "tender"
    if "проект" in f"{lead.product} {lead.message}".lower():
        return "project"
    if known_customer:
        return "regular"
    return "new"


def route_lead(lead: Lead, loads: dict[str, int], known_customer: bool) -> tuple[str, str]:
    """Назначить менеджера и воронку по правилам (география, продукт, нагрузка, тип).

    ``loads`` — текущая загрузка по менеджерам (число активных лидов/сделок). Среди
    подходящих по гео/продукту берём наименее загруженного; нет совпадений —
    распределяем по всем (универсал участвует всегда). Возвращает (менеджер, воронка).
    """
    region = (lead.region or "").lower()
    product = (lead.product or "").lower()

    def matches(m: dict) -> bool:
        if region and any(r in region for r in m["regions"]):
            return True
        if product and any(p in product for p in m["products"]):
            return True
        return False

    candidates = [m for m in MANAGERS if matches(m)] or MANAGERS
    # наименее загруженный (при равенстве — порядок объявления в MANAGERS)
    chosen = min(candidates, key=lambda m: loads.get(m["name"], 0))
    return chosen["name"], choose_funnel(lead, known_customer)


def lead_priority(score: int) -> str:
    """Приоритет будущей сделки по баллу квалификации (для конвертации в Deal)."""
    if score >= 70:
        return "Высокий"
    if score >= QUALIFY_THRESHOLD:
        return "Средний"
    return "Низкий"


async def known_customer(session: AsyncSession, company: str) -> bool:
    """Лид от действующего контрагента? (повышает балл, даёт воронку «постоянные»).

    Общая логика для /qualify, /route и авто-скоринга на входе — раньше жила только
    в routes.py (``_known_customer``), вынесена сюда, чтобы не дублировать запрос.
    """
    company = (company or "").strip()
    if not company:
        return False
    cp = (await session.execute(select(Counterparty).where(Counterparty.name == company))).scalars().first()
    return cp is not None


async def apply_initial_score(lead: Lead, session: AsyncSession) -> None:
    """Проставить стартовый балл/вердикт/причину сразу при создании лида.

    Вызывается во всех точках приёма (POST /leads, интейк веб-формы/почты, звонок,
    кампания) — лидоруб сразу видит приоритет, не дожидаясь явной /qualify. Статус
    лида НЕ меняется (остаётся ``new``): переход в ``qualified`` — по-прежнему
    отдельное действие оператора, которое пересчитает балл повторно.
    """
    known = await known_customer(session, lead.company)
    lead.score, lead.qualification, lead.reason = score_lead(lead, known)


def phone_tail(phone: str | None) -> str:
    """Значащий хвост телефона (9 цифр) — контакты бывают без кода страны."""
    return re.sub(r"\D", "", phone or "")[-9:]


async def find_open_lead_by_phone(session: AsyncSession, phone: str | None) -> Lead | None:
    """Открытый (new/qualified/routed) лид с тем же хвостом телефона, либо None.

    Общий хелпер дедупа интейка — раньше жил только внутри ``on_call_logged``.
    """
    tail = phone_tail(phone)
    if not tail:
        return None
    # ponytail: LIKE-скан по хвосту — при росте базы нормализованная колонка + индекс
    return (
        await session.execute(
            select(Lead).where(
                Lead.phone.isnot(None),
                Lead.phone.like(f"%{tail}"),
                Lead.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().first()


async def find_open_lead_by_email(session: AsyncSession, email: str | None) -> Lead | None:
    """Открытый (new/qualified/routed) лид с тем же e-mail (регистронезависимо), либо None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    return (
        await session.execute(
            select(Lead).where(
                Lead.email.isnot(None),
                Lead.email.ilike(email),
                Lead.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().first()
