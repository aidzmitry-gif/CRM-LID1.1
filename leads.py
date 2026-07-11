"""Lead Qualifier & Router — квалификация и распределение лидов (ФАЗА 1).

Детерминированный движок без модели: скоринг лида по эвристикам (целевой/нет) и
правила распределения на менеджера (география, продукт, нагрузка, тип воронки).
AI-подмодуль (``ai.qualify_lead``) добавляет текстовое обоснование поверх — за
feature-flag, не подменяя этот балл и не переписывая механику (§2.5). Это первый
AI-пилот дорожной карты (Lead Qualifier & Router).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
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

# Ключевой (высокопотенциальный) лид (Цикл 9): его распределяем ради МАКСИМАЛЬНОГО выигрыша —
# к лучшему закрывающему без поправки на загрузку. Порог балла = «Высокий» приоритет (lead_priority).
KEY_SCORE_THRESHOLD = 70
# Сигналы крупного заказа в тексте запроса — крупный объём тоже делает лид ключевым.
KEY_VOLUME_HINTS = ("тонн", "объём", "объем", "вагон", "фура", "оптом", "крупн", "контейнер")


def is_key_lead(lead: Lead) -> bool:
    """Ключевой лид (Цикл 9) — высокий потенциал по данным, что уже есть на лиде (без запроса в БД).

    Высокий балл (≥ порога «Высокий»), тендер (крупные заказы), маркер объёма в тексте или
    действующий клиент/постоянник (``customer_kind``, проставляет Цикл 10). Предикат намеренно
    чистый и дешёвый — считается на списке лидов без N+1 (customer_kind уже на лиде)."""
    if lead.score >= KEY_SCORE_THRESHOLD:
        return True
    if lead.source == "tender":
        return True
    if lead.customer_kind in ("existing", "regular"):
        return True
    text = f"{lead.product} {lead.message}".lower()
    return any(hint in text for hint in KEY_VOLUME_HINTS)

# Причины отказа (Слайс 4) — фиксированный список вместо свободного текста,
# чтобы отчёты по причинам отказа были сравнимы (не «дубль»/«Дубль»/«дублирует»).
# «не сейчас» (Цикл 16) — отказ с созревающим спросом: не терминальная свалка, а рецикл
# с датой авто-возврата (snooze_until) — лид сам вернётся в «Новые», когда спрос дозреет.
REJECT_REASONS = ("не наш профиль", "нет бюджета", "дубль", "конкурент", "не сейчас")


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


# Цена одного активного лида в «единицах конверсии» при умной маршрутизации (Цикл 8):
# менеджер с конверсией выше на 2 п.п. предпочитается, пока он не набрал +1 активный лид.
# Так лид уходит лучшему закрывающему, но перекос выравнивается загрузкой. Потолок — тюнится.
LOAD_PENALTY = 0.02


def route_lead(
    lead: Lead,
    loads: dict[str, int],
    known_customer: bool,
    performance: dict[str, float] | None = None,
    key: bool = False,
) -> tuple[str, str]:
    """Назначить менеджера и воронку по правилам (география, продукт, нагрузка, тип).

    ``loads`` — текущая загрузка по менеджерам (число активных лидов/сделок). Среди
    подходящих по гео/продукту: при наличии истории конверсии (``performance``, Цикл 8)
    берём лучшего закрывающего с поправкой на загрузку (``LOAD_PENALTY``); без истории —
    прежнее правило наименее загруженного. ``key`` (Цикл 9) — ключевой лид: отдаём лучшему
    закрывающему БЕЗ штрафа загрузки (максимальный выигрыш важнее равномерности). Нет
    совпадений — распределяем по всем (универсал участвует всегда). Возвращает (менеджер, воронка).
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
    # есть история конверсии хотя бы у одного кандидата → умная маршрутизация к закрывающему;
    # иначе (холодный старт/нет данных) — прежний баланс по загрузке.
    if performance and any(performance.get(m["name"]) for m in candidates):
        # ключевой лид → лучший закрывающий без штрафа загрузки (макс. выигрыш, Цикл 9)
        penalty = 0.0 if key else LOAD_PENALTY
        chosen = max(
            candidates,
            key=lambda m: performance.get(m["name"], 0.0) - loads.get(m["name"], 0) * penalty,
        )
    else:
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


# Рабочее окно лидоруба в НАИВНОМ UTC (как created_at/first_action_at): Минск 9-18 = UTC 6-15
# (Беларусь живёт без перевода часов). Цикл 14: факт «Реакция» считается рабочими минутами —
# иначе один ночной лид, разобранный в 9:05, сжигает метрику на весь день.
WORKDAY_START_HOUR_UTC = 6
WORKDAY_END_HOUR_UTC = 15


def working_minutes_between(start: datetime, end: datetime) -> float:
    """Минуты РАБОЧИМИ часами (9-18 Минска, ежедневно) между двумя наивными UTC-моментами.

    Ночь не считается: лид, пришедший в 23:00 и разобранный в 9:10, ждал 10 рабочих
    минут, а не 10 часов. Идём по дням, суммируя пересечение интервала с рабочим окном.
    # ponytail: выходные считаются рабочими — отдел работает и по субботам; появится
    # график смен — вынести окно в настройку. O(дней) — лиды свежие, норм.
    """
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        day_start = cur.replace(hour=WORKDAY_START_HOUR_UTC, minute=0, second=0, microsecond=0)
        day_end = cur.replace(hour=WORKDAY_END_HOUR_UTC, minute=0, second=0, microsecond=0)
        seg_start = max(cur, day_start)
        seg_end = min(end, day_end)
        if seg_end > seg_start:
            total += (seg_end - seg_start).total_seconds() / 60
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return total


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


async def find_last_rejected_by_contact(
    session: AsyncSession, phone: str | None, email: str | None, company: str | None = None
) -> Lead | None:
    """Последний ОТКЛОНЁННЫЙ лид того же контакта (телефон-хвост/e-mail) — реанимация (Цикл 12).

    Чтобы новый лид от ранее отклонённого контакта не терял память об отказе: продавец видит,
    что уже был отказ и его причину, а не работает вслепую. Терминальные rejected в дедуп
    открытых лидов не попадают — ищем их отдельно, самый свежий. ``company`` — тот же гард, что
    в дедупе: отказ ДРУГОЙ компании с общим телефоном не приписываем (иначе ложный «был отказ»).
    """
    tail = phone_tail(phone)
    email_n = (email or "").strip().lower()
    conds = []
    if tail:
        conds.append(and_(Lead.phone.isnot(None), Lead.phone.like(f"%{tail}")))
    if email_n:
        conds.append(and_(Lead.email.isnot(None), func.lower(Lead.email) == email_n))
    if not conds:
        return None
    cands = (
        await session.execute(
            select(Lead).where(Lead.status == "rejected", or_(*conds)).order_by(Lead.id.desc())
        )
    ).scalars().all()
    return next((c for c in cands if not companies_conflict(c.company, company)), None)


def cancel_pending_wake(prior_rej: Lead | None) -> bool:
    """Погасить авто-возврат спящего «не сейчас» лида — контакт уже вернулся новым лидом.

    Иначе спящий лид проснётся (wake-on-read) отдельной карточкой того же клиента: два
    лидоруба звонят одному контакту (фантомный дубль, хвост ревью Ц16). Новый лид уже несёт
    ``revived_from_id`` на этот отказ — память сохранена, а дубль-пробуждение отменяем.
    Возвращает True, если что-то погашено (для лога/тестов)."""
    if prior_rej is not None and prior_rej.reject_reason == "не сейчас" and prior_rej.snooze_until is not None:
        prior_rej.snooze_until = None
        return True
    return False


async def golden_counterparty_id(session: AsyncSession, cp_id: int) -> int:
    """Идти по цепочке ``merged_into_id`` до эталона (golden record) контрагента.

    Дубль архивируется и ссылается на эталон (``core.services.mdm.merge``); лид должен
    привязываться к эталону, а не к слитому дублю. Защита от цикла — множество посещённых.
    """
    cp = await session.get(Counterparty, cp_id)
    seen: set[int] = set()
    while cp is not None and cp.merged_into_id is not None and cp.id not in seen:
        seen.add(cp.id)
        cp = await session.get(Counterparty, cp.merged_into_id)
    return cp.id if cp is not None else cp_id


async def resolve_customer(session: AsyncSession, lead: Lead) -> None:
    """Привязать лид к эталонному контрагенту и пометить тип клиента (Цикл 10).

    Резолв против существующих клиентов, чтобы обращение действующего клиента не выглядело
    холодным лидом: (1) по контакту (телефон/e-mail) → его контрагент; (2) иначе по ТОЧНОМУ
    имени активного эталона. Fuzzy-совпадение имени НЕ авто-привязываем: в MDM это «кандидаты
    на approval, человек-в-контуре» (``mdm.fuzzy_candidates``, §7.2) — авто-склейка похожих
    имён («…Плюс»/филиал) пометила бы разные компании как одну. Найденный id приводится к
    golden record. ``customer_kind``: ``regular`` если по этому контрагенту уже были лиды
    (постоянник), иначе ``existing``. Вызывается ПОСЛЕ flush (нужен ``lead.id`` для исключения
    себя из подсчёта). Не найдено — лид остаётся новым/холодным (``customer_kind=""``).
    """
    from core.services import mdm

    cp_id: int | None = None
    contact = await mdm.find_contact(session, phone=lead.phone, email=lead.email)
    if contact is not None and contact.counterparty_id is not None:
        cp_id = contact.counterparty_id
    if cp_id is None:
        company = (lead.company or "").strip()
        if company:
            exact = (
                await session.execute(
                    select(Counterparty).where(
                        Counterparty.name == company, Counterparty.is_active.is_(True)
                    )
                )
            ).scalars().first()
            if exact is not None:
                cp_id = exact.id
    if cp_id is None:
        return
    cp_id = await golden_counterparty_id(session, cp_id)
    lead.counterparty_id = cp_id
    prior = (
        await session.execute(
            select(func.count()).where(Lead.counterparty_id == cp_id, Lead.id != lead.id)
        )
    ).scalar_one()
    lead.customer_kind = "regular" if prior > 0 else "existing"


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
    """Значащий хвост телефона (9 цифр) — контакты бывают без кода страны.

    Хвост короче 7 цифр (обрезанный/битый ввод) не годится для дедупа:
    LIKE-матч по нему цеплял бы чужие номера с тем же окончанием.
    """
    tail = re.sub(r"\D", "", phone or "")[-9:]
    return tail if len(tail) >= 7 else ""


def companies_conflict(a: str | None, b: str | None) -> bool:
    """Разные компании при одинаковом контакте (Цикл 12, gap #3): защита от ложного дедупа.

    Один телефон/e-mail бывает у РАЗНЫХ компаний (общий коммутатор, смена SIM, секретарь).
    Конфликт только если оба имени непустые и ни одно не является подстрокой другого
    (``Ромашка`` vs ``ООО Ромашка`` — не конфликт; ``Ромашка`` vs ``Стройторг`` — конфликт).
    Пустое имя → не конфликт (дозаписываем, как раньше).
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a not in b and b not in a


async def find_open_lead_by_phone(
    session: AsyncSession, phone: str | None, company: str | None = None
) -> Lead | None:
    """Открытый (new/qualified/routed) лид с тем же хвостом телефона, либо None.

    ``company`` (Цикл 12) — если у кандидата другая компания (конфликт), это НЕ дубль
    (тот же номер у разных фирм): вернём None, чтобы завести отдельный лид, а не портить чужой.
    """
    tail = phone_tail(phone)
    if not tail:
        return None
    # ponytail: LIKE-скан по хвосту — при росте базы нормализованная колонка + индекс.
    # Берём ВСЕХ кандидатов и первого совместимого по компании (самого свежего): один хвост
    # может быть у лидов разных фирм (общий коммутатор) — .first() мог бы вернуть чужого и
    # ложно решить «не дубль», заведя дубль своей же компании.
    cands = (
        await session.execute(
            select(Lead)
            .where(
                Lead.phone.isnot(None),
                Lead.phone.like(f"%{tail}"),
                Lead.status.in_(OPEN_STATUSES),
            )
            .order_by(Lead.id.desc())
        )
    ).scalars().all()
    return next((c for c in cands if not companies_conflict(c.company, company)), None)


async def find_open_lead_by_email(
    session: AsyncSession, email: str | None, company: str | None = None
) -> Lead | None:
    """Открытый (new/qualified/routed) лид с тем же e-mail (регистронезависимо), либо None.

    ``company`` (Цикл 12) — как в ``find_open_lead_by_phone``: конфликт компаний → не дубль.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    cands = (
        await session.execute(
            select(Lead)
            .where(
                Lead.email.isnot(None),
                # точное сравнение, НЕ ilike: в LIKE-паттерне `_`/`%` — wildcard'ы,
                # а `_` в адресах сплошь и рядом (ivan_petrov@) → ложные дубли
                func.lower(Lead.email) == email,
                Lead.status.in_(OPEN_STATUSES),
            )
            .order_by(Lead.id.desc())
        )
    ).scalars().all()
    # как в find_open_lead_by_phone: первый совместимый по компании, не .first() вслепую
    return next((c for c in cands if not companies_conflict(c.company, company)), None)
