"""AI-подмодуль Leads (Итерация 1) — за feature-flag, через шлюз ядра.

AI-обоснование квалификации лида поверх детерминированного скоринга
(``leads.score_lead``): балл и вердикт считает движок, AI лишь поясняет вывод и
предлагает следующее действие. Обращается к общему LLM-шлюзу
(``core.services.llm``), не к модели напрямую; вызов сопровождается доменным
событием (трассировка AI-действия в audit, §3.3).
"""
from __future__ import annotations

from modules.leads.models import Lead


async def qualify_lead(gateway, lead: Lead, score: int, verdict: str) -> str:
    """AI-обоснование квалификации лида (Lead Qualifier & Router, Итерация 1).

    Поверх детерминированного скоринга (``leads.score_lead``): даёт краткое
    пояснение вывода и рекомендацию по работе с лидом. Вызывается за feature-flag.
    """
    system = "Ты — AI-квалификатор лидов отдела продаж. Кратко поясни оценку на русском."
    verdict_ru = "целевой" if verdict == "target" else "нецелевой"
    prompt = (
        f"Лид из канала «{lead.source}», компания «{lead.company or '—'}», "
        f"регион «{lead.region or '—'}», интерес «{lead.product or '—'}». "
        f"Сообщение: «{lead.message or '—'}». Оценка {score}/100, вердикт: {verdict_ru}. "
        f"Поясни вывод и предложи следующее действие."
    )
    return await gateway.complete(prompt, system=system, kind="qualify")
