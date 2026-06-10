"""Lead Qualifier & Router — квалификация и распределение лидов (ФАЗА 1).

Детерминированный движок без модели: скоринг лида по эвристикам (целевой/нет) и
правила распределения на менеджера (география, продукт, нагрузка, тип воронки).
AI-подмодуль (``ai.qualify_lead``) добавляет текстовое обоснование поверх — за
feature-flag, не подменяя этот балл и не переписывая механику (§2.5). Это первый
AI-пилот дорожной карты (Lead Qualifier & Router).
"""
from __future__ import annotations

from modules.leads.models import Lead

# Источники лида (каналы приёма): сайт/лендинг, мессенджеры, e-mail, телефония, тендеры.
LEAD_SOURCES = ("site", "telegram", "whatsapp", "email", "phone", "tender")

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
