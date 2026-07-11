"""HTTP-API модуля Leads. Монтируется ядром под префиксом ``/leads``.

Перенесено из репозитория CRM (бывшие эндпоинты ``/sales/leads*``). Главное
архитектурное отличие после выноса: конвертация не создаёт сделку напрямую —
модуль публикует ``leads.lead.converted``, сделку создаёт модуль sales (§2.4/§2.5).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Counterparty
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.leads.ai import qualify_lead
from modules.leads.leads import (
    MANAGERS,
    REJECT_REASONS,
    apply_initial_score,
    choose_funnel,
    find_last_rejected_by_contact,
    find_open_lead_by_email,
    find_open_lead_by_phone,
    is_key_lead,
    known_customer,
    lead_priority,
    resolve_customer,
    route_lead,
    score_lead,
)
from modules.leads.models import Lead, LeadAttachment, LeadItem, LeadPlan
from modules.leads.schemas import (
    LeadAttachmentIn,
    LeadAttachmentOut,
    LeadBulkExpressOut,
    LeadConvertOut,
    LeadCreate,
    LeadHandoffStatOut,
    LeadItemIn,
    LeadItemOut,
    LeadOut,
    LeadPlanIn,
    LeadPlanOut,
    LeadQualifyOut,
    LeadRejectOut,
    LeadRouteOut,
    LeadSourceStatOut,
    LinkContactIn,
    LinkContactOut,
    ManagerOut,
    RejectIn,
    RouteIn,
)
from modules.leads.storage import (
    AttachmentRejected,
    decode_data_url,
    read_attachment,
    save_attachment,
)

router = APIRouter(tags=["leads"])


def _utcnow() -> datetime:
    # наивный UTC — единообразно для SQLite и PostgreSQL (см. modules/sales/repository.py)
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _attach_item_totals(session: AsyncSession, leads: list[Lead]) -> list[Lead]:
    """Проставить лидам ``items_count``/``items_total`` (сумма qty*price) для LeadOut.

    Один агрегат по всем лидам сразу (без N+1): карточка/drawer показывают «КП: N поз.
    на X BYN» без отдельного запроса. Значения кладём как transient-атрибуты — LeadOut
    (from_attributes) их читает; для лидов без позиций остаются дефолтные 0."""
    ids = [lead.id for lead in leads]
    totals: dict[int, tuple[int, float]] = {}
    if ids:
        rows = (
            await session.execute(
                select(
                    LeadItem.lead_id,
                    func.count(),
                    func.coalesce(func.sum(LeadItem.qty * LeadItem.price), 0),
                )
                .where(LeadItem.lead_id.in_(ids))
                .group_by(LeadItem.lead_id)
            )
        ).all()
        totals = {lead_id: (count, float(total)) for lead_id, count, total in rows}
    for lead in leads:
        count, total = totals.get(lead.id, (0, 0.0))
        lead.items_count = count
        lead.items_total = total
        lead.is_key = is_key_lead(lead)  # Цикл 9: производный флаг ключевого лида (бейдж 🔑)
    return leads


def _mark_first_action(lead: Lead) -> None:
    """SLA первой реакции: проставить время первого действия лидоруба (один раз)."""
    if lead.first_action_at is None:
        lead.first_action_at = _utcnow()


async def _compute_score(lead: Lead, session: AsyncSession) -> tuple[int, str, str]:
    """Пересчитать скоринг лида (без мутации) → (балл, вердикт, причина).

    Отделено от ``_apply_score``, чтобы /express мог проверить вердикт ДО того, как
    менять лид — иначе неудачный (non-target) экспресс оставил бы лида в статусе
    ``qualified`` без коммита, но видимым в той же сессии (тестовый клиент делит
    сессию между запросами внутри теста).
    """
    known = await known_customer(session, lead.company)
    return score_lead(lead, known)


def _apply_score(lead: Lead, score: int, verdict: str, reason: str) -> None:
    """Проставить на лиде посчитанный скоринг: балл/вердикт/причина, статус ``qualified``
    (если был ``new``), first_action. Общая логика для /qualify и /express (Цикл 2)."""
    lead.score = score
    lead.qualification = verdict
    lead.reason = reason
    if lead.status == "new":
        lead.status = "qualified"
    _mark_first_action(lead)


async def _emit_qualified(
    lead: Lead, score: int, verdict: str, core: Core, session: AsyncSession
) -> tuple[str | None, str | None]:
    """AI-обоснование (если включён) + событие ``leads.lead.qualified``/``ai.lead.qualified``.

    Возвращает (rationale, model). Общая логика для /qualify и /express (Цикл 2).
    """
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
    return rationale, model


async def _resolve_manager(
    lead: Lead, session: AsyncSession, manual_manager: str
) -> tuple[str, str, str]:
    """Выбрать менеджера, воронку и обоснование — вручную либо умной маршрутизацией (Цикл 8).

    ``manual_manager`` должен совпадать с одним из известных ``MANAGERS``, иначе 422 (не
    даём привязать лид к несуществующему/опечатанному имени). Авто-режим учитывает историю
    конверсии менеджеров (``_manager_performance``): лид уходит лучшему закрывающему с
    поправкой на загрузку. Возвращает (менеджер, воронка, обоснование выбора для оператора).
    Общая логика для /route и /express (Цикл 2).
    """
    known = await known_customer(session, lead.company)
    if manual_manager:
        if manual_manager not in {m["name"] for m in MANAGERS}:
            raise HTTPException(status_code=422, detail=f"Неизвестный менеджер: {manual_manager}")
        return manual_manager, choose_funnel(lead, known), f"Ручной выбор: {manual_manager}"
    loads = await _manager_loads(session)
    perf = await _manager_performance(session)
    rates = {name: rate for name, (rate, _) in perf.items()}
    # Баланс для умной маршрутизации — по НЕДАВНЕМУ объёму (routed+converted за окно), а не
    # только открытым лидам: иначе нагрузка быстрого закрывающего «испаряется» при конвертации
    # и штраф LOAD_PENALTY его не догоняет (лид уходит одному, остальные простаивают). Открытая
    # загрузка (``loads``) остаётся для ноты оператору — она нагляднее как «сколько сейчас висит».
    volume = {name: assigned for name, (_, assigned) in perf.items()}
    balance = {**loads, **volume}
    key = is_key_lead(lead)  # ключевой лид → лучшему закрывающему без штрафа загрузки (Цикл 9)
    manager, funnel = route_lead(lead, balance, known, rates, key=key)
    return manager, funnel, _route_rationale(manager, loads, rates, key)


def _route_rationale(
    manager: str, loads: dict[str, int], performance: dict[str, float], key: bool = False
) -> str:
    """Человекочитаемое «почему этот менеджер» — для ноты оператору (Цикл 8/9)."""
    load = loads.get(manager, 0)
    conv = performance.get(manager)
    if key:
        pref = "🔑 ключевой → "
        return f"{pref}{manager}: конверсия {round(conv * 100)}%" if conv else f"{pref}{manager}"
    if conv:
        return f"{manager}: конверсия {round(conv * 100)}%, загрузка {load}"
    return f"{manager}: по правилам (гео/продукт), загрузка {load}"


def _apply_route(
    lead: Lead,
    manager: str,
    funnel: str,
    next_step_at: datetime | None,
    next_step_note: str | None,
) -> None:
    """Проставить раздачу на лиде: менеджер/воронка/статус routed/first_action, опц. след. шаг."""
    lead.assigned_to = manager
    lead.funnel = funnel
    lead.status = "routed"
    _mark_first_action(lead)
    if next_step_at is not None:
        lead.next_step_at = next_step_at
        lead.next_step_note = next_step_note or ""


def _emit_routed(lead: Lead, manager: str, funnel: str, manual: bool, core: Core, session: AsyncSession) -> None:
    """Событие ``leads.lead.routed`` (+ флаг ``manual`` для аудита). Общая логика /route и /express."""
    core.event_bus.emit(
        session,
        "leads.lead.routed",
        {
            "lead_id": lead.id, "assigned_to": manager, "funnel": funnel,
            "manual": manual, "entity_ref": f"lead:{lead.id}",
        },
    )


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


async def _manager_performance(session: AsyncSession, days: int = 90) -> dict[str, tuple[float, int]]:
    """История менеджеров (Цикл 8) → ``{имя: (конверсия, недавний объём)}``.

    За последние ``days`` дней по каждому менеджеру: конверсия = converted/(routed+converted)
    и объём = число переданных лидов (для баланса нагрузки, чтобы конвертнутые лиды тоже
    считались). Пустой словарь при холодном старте → маршрутизация падает на баланс загрузки.
    """
    since = _utcnow() - timedelta(days=days)
    assigned = func.count()
    converted = func.sum(case((Lead.status == "converted", 1), else_=0))
    rows = (
        await session.execute(
            select(Lead.assigned_to, assigned, converted)
            .where(
                Lead.created_at >= since,
                Lead.assigned_to != "",
                Lead.status.in_(("routed", "converted")),
            )
            .group_by(Lead.assigned_to)
        )
    ).all()
    return {name: (conv / asg if asg else 0.0, asg) for name, asg, conv in rows}


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
    leads = list((await session.execute(query)).scalars().all())
    return await _attach_item_totals(session, leads)


@router.post("", response_model=LeadOut, status_code=201)
async def create_lead(
    payload: LeadCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Принять лид из канала (сайт/мессенджер/e-mail/телефония/тендер) → событие в шину.

    Дедуп: открытый лид (new/qualified/routed) с тем же телефоном или e-mail — 409
    вместо создания дубля (ручной интейк лидорубом, в отличие от веб-формы/почты
    не дописывает обращение автоматически — оператор решает сам, открыв дубль).
    """
    dup = await find_open_lead_by_phone(session, payload.phone, payload.company)
    if dup is None:
        dup = await find_open_lead_by_email(session, payload.email, payload.company)
    if dup is not None:
        raise HTTPException(
            status_code=409,
            detail={"duplicate_of": dup.id, "message": f"Дубль лида #{dup.id}"},
        )

    lead = Lead(**payload.model_dump())
    session.add(lead)
    await apply_initial_score(lead, session)  # балл сразу на входе, статус остаётся new
    await session.flush()
    await resolve_customer(session, lead)  # Цикл 10: резолв против существующих клиентов
    prior_rej = await find_last_rejected_by_contact(session, lead.phone, lead.email, lead.company)
    if prior_rej is not None:
        lead.revived_from_id = prior_rej.id  # Цикл 12: память об отказе
    core.event_bus.emit(
        session,
        "leads.lead.received",
        {
            "lead_id": lead.id,
            "source": lead.source,
            "entity_ref": f"lead:{lead.id}",
            "utm_source": lead.utm_source,
            "utm_medium": lead.utm_medium,
            "utm_campaign": lead.utm_campaign,
        },
    )
    await session.commit()
    await session.refresh(lead)  # created_at — server_default, нужен свежий снимок для LeadOut
    lead.is_key = is_key_lead(lead)  # Цикл 9: производный флаг для ответа (create минует _attach)
    return lead


@router.get("/managers", response_model=list[ManagerOut])
async def list_managers(session: AsyncSession = Depends(get_session)):
    """Менеджеры для ручной раздачи: специализация (гео/продукт) + текущая загрузка.

    ДО ``/{lead_id}`` в файле нарочно — иначе FastAPI примет ``managers`` за
    ``lead_id`` (422 «не int») раньше, чем дойдёт до этого маршрута.
    """
    loads = await _manager_loads(session)
    return [
        ManagerOut(name=m["name"], regions=m["regions"], products=m["products"], load=loads.get(m["name"], 0))
        for m in MANAGERS
    ]


@router.get("/stats/sources", response_model=list[LeadSourceStatOut])
async def source_stats(days: int = 30, session: AsyncSession = Depends(get_session)):
    """Отчёт качества источника/кампании (Цикл 4) — за последние ``days`` дней.

    ДО ``/{lead_id}`` в файле нарочно (как ``/managers``) — иначе FastAPI примет
    ``stats`` за ``lead_id``. Один SQL-запрос с group by (source, utm_campaign) —
    без выгрузки всех лидов в память.
    """
    since = _utcnow() - timedelta(days=days)
    item_totals = _lead_item_totals_subquery()
    lead_total = func.coalesce(item_totals.c.lead_total, 0)
    target = func.sum(case((Lead.qualification == "target", 1), else_=0))
    converted = func.sum(case((Lead.status == "converted", 1), else_=0))
    rejected = func.sum(case((Lead.status == "rejected", 1), else_=0))
    # Σ КП только сконвертированных лидов (Цикл 7) — деньги, отданные продавцам из источника.
    pipeline = func.sum(case((Lead.status == "converted", lead_total), else_=0))
    total = func.count()
    rows = (
        await session.execute(
            select(
                Lead.source,
                Lead.utm_campaign,
                total,
                target,
                converted,
                rejected,
                func.avg(Lead.score),
                pipeline,
            )
            .select_from(Lead)
            .outerjoin(item_totals, item_totals.c.lead_id == Lead.id)
            .where(Lead.created_at >= since)
            .group_by(Lead.source, Lead.utm_campaign)
            .order_by(total.desc())
        )
    ).all()
    return [
        LeadSourceStatOut(
            source=source,
            utm_campaign=utm_campaign,
            total=n,
            target=n_target,
            converted=n_converted,
            rejected=n_rejected,
            avg_score=round(float(avg_score or 0), 1),
            target_pct=round(n_target / n * 100, 1) if n else 0.0,
            conversion_pct=round(n_converted / n * 100, 1) if n else 0.0,
            pipeline=round(float(n_pipeline or 0), 2),
        )
        for source, utm_campaign, n, n_target, n_converted, n_rejected, avg_score, n_pipeline in rows
    ]


def _lead_item_totals_subquery():
    """Подзапрос «сумма КП на лид» (Σ qty*price по позициям) — 1:1 к лиду для outerjoin.

    Агрегируем позиции ПО лиду заранее, чтобы внешний group by (по источнику/менеджеру)
    суммировал уже готовые суммы лидов, а не задваивал строки при join к lead_item."""
    return (
        select(
            LeadItem.lead_id.label("lead_id"),
            func.sum(LeadItem.qty * LeadItem.price).label("lead_total"),
        )
        .group_by(LeadItem.lead_id)
        .subquery()
    )


@router.get("/stats/handoffs", response_model=list[LeadHandoffStatOut])
async def handoff_stats(days: int = 30, session: AsyncSession = Depends(get_session)):
    """Скорборд передач лидоруба продавцам (Цикл 7) — за последние ``days`` дней.

    По каждому продавцу: сколько лидов лидоруб ему передал (routed/converted), сколько тот
    довёл до сделки и на какую сумму КП. Показывает вклад специалиста в план каждого
    продавца деньгами. ДО ``/{lead_id}`` в файле нарочно (как ``/stats/sources``).
    """
    since = _utcnow() - timedelta(days=days)
    item_totals = _lead_item_totals_subquery()
    lead_total = func.coalesce(item_totals.c.lead_total, 0)
    assigned = func.count()
    converted = func.sum(case((Lead.status == "converted", 1), else_=0))
    pipeline = func.sum(case((Lead.status == "converted", lead_total), else_=0))
    rows = (
        await session.execute(
            select(Lead.assigned_to, assigned, converted, pipeline)
            .select_from(Lead)
            .outerjoin(item_totals, item_totals.c.lead_id == Lead.id)
            .where(
                Lead.created_at >= since,
                Lead.assigned_to != "",
                Lead.status.in_(("routed", "converted")),
            )
            .group_by(Lead.assigned_to)
            .order_by(pipeline.desc())
        )
    ).all()
    return [
        LeadHandoffStatOut(
            manager=manager,
            assigned=n_assigned,
            converted=n_converted,
            pipeline=round(float(n_pipeline or 0), 2),
            conversion_pct=round(n_converted / n_assigned * 100, 1) if n_assigned else 0.0,
        )
        for manager, n_assigned, n_converted, n_pipeline in rows
    ]


async def _get_plan(session: AsyncSession) -> LeadPlan:
    """Дневная норма лидоруба (одна строка ``period='daily'``); создаём при первом обращении."""
    plan = (
        await session.execute(select(LeadPlan).where(LeadPlan.period == "daily"))
    ).scalars().first()
    if plan is None:
        plan = LeadPlan(period="daily")
        session.add(plan)
        try:
            await session.flush()
        except IntegrityError:
            # гонка первого создания строки нормы (uq_lead_plan_period) — другой запрос
            # успел раньше; откатываем и перечитываем (тот же паттерн, что в sales/procurement).
            await session.rollback()
            plan = (
                await session.execute(select(LeadPlan).where(LeadPlan.period == "daily"))
            ).scalars().first()
    return plan


async def _plan_facts(session: AsyncSession) -> tuple[int, int, int, int | None]:
    """Факт лидоруба за сегодня → (обработано, целевых передано, доведено, ср. реакция мин).

    «Сегодня» — наивный UTC-день (как ``created_at``/``first_action_at``). Обработано =
    первое действие сегодня; целевых передано = из них целевые, ушедшие в routed/converted;
    доведено = converted_at сегодня; реакция = ср. (first_action_at − created_at) по обработанным.
    """
    start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    acted = (
        await session.execute(
            select(Lead.created_at, Lead.first_action_at, Lead.qualification, Lead.status).where(
                Lead.first_action_at >= start, Lead.first_action_at < end
            )
        )
    ).all()
    leads_fact = len(acted)
    qualified_fact = sum(
        1 for _, _, q, s in acted if q == "target" and s in ("routed", "converted")
    )
    reaction_mins = [
        (fa - ca).total_seconds() / 60
        for ca, fa, _, _ in acted
        if ca is not None and fa is not None and fa >= ca
    ]
    reaction_fact_min = round(sum(reaction_mins) / len(reaction_mins)) if reaction_mins else None
    converted_fact = (
        await session.execute(
            select(func.count()).where(Lead.converted_at >= start, Lead.converted_at < end)
        )
    ).scalar_one()
    return leads_fact, qualified_fact, converted_fact, reaction_fact_min


def _plan_out(plan: LeadPlan, facts: tuple[int, int, int, int | None]) -> LeadPlanOut:
    leads_fact, qualified_fact, converted_fact, reaction_fact_min = facts
    return LeadPlanOut(
        leads_target=plan.leads_target,
        qualified_target=plan.qualified_target,
        converted_target=plan.converted_target,
        reaction_target_min=plan.reaction_target_min,
        leads_fact=leads_fact,
        qualified_fact=qualified_fact,
        converted_fact=converted_fact,
        reaction_fact_min=reaction_fact_min,
    )


@router.get("/plan", response_model=LeadPlanOut)
async def get_plan(session: AsyncSession = Depends(get_session)):
    """План/факт лидоруба за сегодня (Цикл 5): дневная норма + факт из лидов.

    ДО ``/{lead_id}`` в файле нарочно (как ``/managers``/``/stats``) — иначе ``plan``
    примут за ``lead_id``.
    """
    plan = await _get_plan(session)
    facts = await _plan_facts(session)
    await session.commit()  # _get_plan мог создать строку нормы
    return _plan_out(plan, facts)


@router.put("/plan", response_model=LeadPlanOut)
async def set_plan(payload: LeadPlanIn, session: AsyncSession = Depends(get_session)):
    """Задать дневную норму лидоруба (Цикл 5) — правит РОП/лидоруб; факт пересчитывается."""
    plan = await _get_plan(session)
    plan.leads_target = payload.leads_target
    plan.qualified_target = payload.qualified_target
    plan.converted_target = payload.converted_target
    plan.reaction_target_min = payload.reaction_target_min
    plan.updated_at = _utcnow()
    await session.commit()
    facts = await _plan_facts(session)
    return _plan_out(plan, facts)


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: int, session: AsyncSession = Depends(get_session)):
    """Один лид по id."""
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    (await _attach_item_totals(session, [lead]))
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

    score, verdict, reason = await _compute_score(lead, session)
    _apply_score(lead, score, verdict, reason)
    rationale, model = await _emit_qualified(lead, score, verdict, core, session)
    await session.commit()
    return LeadQualifyOut(
        id=lead.id, status=lead.status, score=score, qualification=verdict,
        reason=reason, ai_rationale=rationale, model=model,
    )


@router.post("/{lead_id}/reject", response_model=LeadRejectOut)
async def reject_lead(
    lead_id: int,
    payload: RejectIn,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Отклонить лид (Слайс 4) — терминальный статус ``rejected`` + причина.

    ``reason`` — один из ``REJECT_REASONS`` (не наш профиль/нет бюджета/дубль/
    конкурент), иначе 422: фиксированный список, чтобы отчёты по причинам отказа
    были сравнимы. Уже сконвертированный или уже отклонённый лид — 409.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status in ("converted", "rejected"):
        raise HTTPException(status_code=409, detail=f"Лид уже в терминальном статусе: {lead.status}")
    if payload.reason not in REJECT_REASONS:
        raise HTTPException(status_code=422, detail=f"Неизвестная причина отказа: {payload.reason}")

    lead.status = "rejected"
    lead.reject_reason = payload.reason
    _mark_first_action(lead)
    core.event_bus.emit(
        session,
        "leads.lead.rejected",
        {"lead_id": lead.id, "reason": payload.reason, "entity_ref": f"lead:{lead.id}"},
    )
    await session.commit()
    return LeadRejectOut(id=lead.id, status=lead.status, reject_reason=lead.reject_reason)


@router.post("/{lead_id}/route", response_model=LeadRouteOut)
async def route(
    lead_id: int,
    payload: RouteIn | None = Body(default=None),
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Распределить лид на менеджера — по правилам или вручную.

    Без тела (или пустой ``assigned_to``) — прежние авто-правила (география/
    продукт/нагрузка). С ``{"assigned_to": "<имя>"}`` — явный выбор оператора
    (список менеджеров с загрузкой — ``GET /leads/managers``); имя должно
    совпадать с одним из известных менеджеров, иначе 422 (не даём привязать
    лид к несуществующему/опечатанному имени). Воронка при ручном выборе
    считается теми же правилами (``choose_funnel``), что и при авто-режиме.

    ``next_step_at``/``next_step_note`` (Слайс 4, опционально) — срок и заметка
    для продавца, ставятся вместе с раздачей независимо от авто/ручного режима.

    Публикует ``leads.lead.routed`` (то же событие в обоих режимах, + флаг
    ``manual`` для аудита). Уже сконвертированный лид — 409.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status == "converted":
        raise HTTPException(status_code=409, detail="Лид уже сконвертирован в сделку")
    if lead.status == "rejected":
        raise HTTPException(status_code=409, detail="Лид отклонён — раздача недоступна")

    manual_manager = (payload.assigned_to or "").strip() if payload else ""
    manager, funnel, rationale = await _resolve_manager(lead, session, manual_manager)
    _apply_route(
        lead, manager, funnel,
        payload.next_step_at if payload else None,
        payload.next_step_note if payload else None,
    )
    _emit_routed(lead, manager, funnel, bool(manual_manager), core, session)
    await session.commit()
    return LeadRouteOut(
        id=lead.id, status=lead.status, assigned_to=manager, funnel=funnel, rationale=rationale
    )


@router.post("/{lead_id}/express", response_model=LeadOut)
async def express_lead(
    lead_id: int,
    payload: RouteIn | None = Body(default=None),
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Экспресс-передача лида продавцу (Цикл 2) — квалификация + раздача + следующий шаг
    одним действием в одной транзакции, вместо последовательных /qualify → /route.

    Допустим только из ``new``/``qualified`` (иначе 409, как в /route). Тело — как у
    /route (``assigned_to``/``next_step_at``/``next_step_note``, все опциональны):
    ``assigned_to`` — ручной выбор менеджера с проверкой по ``MANAGERS`` (422 на
    неизвестного), без него — авто-правила (``route_lead``). Скоринг пересчитывается
    заново (как в /qualify); если вердикт оказался нецелевым — 422: экспресс не
    подменяет ручную квалификацию сомнительных лидов, только явно целевых.

    Эмитит оба события (``leads.lead.qualified``/``ai.lead.qualified`` и
    ``leads.lead.routed``) — контракт шины не меняется, подписчики те же, что и на
    последовательные /qualify + /route.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status == "converted":
        raise HTTPException(status_code=409, detail="Лид уже сконвертирован в сделку")
    if lead.status == "rejected":
        raise HTTPException(status_code=409, detail="Лид отклонён — экспресс недоступен")
    if lead.status == "routed":
        raise HTTPException(status_code=409, detail="Лид уже распределён — экспресс недоступен")

    score, verdict, reason = await _compute_score(lead, session)
    if verdict != "target":
        raise HTTPException(
            status_code=422,
            detail=f"Лид не целевой (балл {score}) — экспресс недоступен, квалифицируй вручную",
        )
    _apply_score(lead, score, verdict, reason)
    await _emit_qualified(lead, score, verdict, core, session)

    manual_manager = (payload.assigned_to or "").strip() if payload else ""
    manager, funnel, _rationale = await _resolve_manager(lead, session, manual_manager)
    _apply_route(
        lead, manager, funnel,
        payload.next_step_at if payload else None,
        payload.next_step_note if payload else None,
    )
    _emit_routed(lead, manager, funnel, bool(manual_manager), core, session)

    await session.commit()
    (await _attach_item_totals(session, [lead]))
    return lead


@router.post("/express-bulk", response_model=LeadBulkExpressOut)
async def express_bulk(
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Разобрать все целевые новые лиды одним действием (Цикл 6) — конвейер лидоруба.

    Проходит по новым лидам: пересчитывает скоринг, целевые — квалифицирует и распределяет
    авто-правилами (загрузка балансируется внутри пачки: ``_resolve_manager`` видит уже
    распределённых через autoflush), нецелевые — пропускает (их разбирают вручную). Одна
    транзакция вместо N кликов qualify→route по каждой карточке. Оба события шины эмитятся
    на каждый распределённый лид — контракт тот же, что у одиночного /express.

    ДО ``/{lead_id}/...`` в файле нарочно — ``express-bulk`` не должен пойматься как lead_id.
    """
    new_leads = (
        await session.execute(select(Lead).where(Lead.status == "new").order_by(Lead.id))
    ).scalars().all()
    expressed: list[int] = []
    skipped_non_target = 0
    for lead in new_leads:
        score, verdict, reason = await _compute_score(lead, session)
        if verdict != "target":
            skipped_non_target += 1
            continue
        _apply_score(lead, score, verdict, reason)
        await _emit_qualified(lead, score, verdict, core, session)
        manager, funnel, _rationale = await _resolve_manager(lead, session, "")
        _apply_route(lead, manager, funnel, None, None)
        _emit_routed(lead, manager, funnel, False, core, session)
        expressed.append(lead.id)
    await session.commit()
    return LeadBulkExpressOut(expressed=expressed, skipped_non_target=skipped_non_target)


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
    lead.converted_at = _utcnow()  # момент конвертации — для дневного план/факта (Цикл 5)
    # Позиции подобранного КП — в payload события (аддитивно: подписчик sales со старым
    # контрактом их игнорирует; перенос позиций в сделку делает фронт цепочкой «В сделку + счёт»).
    item_rows = (
        await session.execute(
            select(LeadItem).where(LeadItem.lead_id == lead.id).order_by(LeadItem.id)
        )
    ).scalars().all()
    items = [
        {
            "sku_id": it.sku_id,
            "sku_code": it.sku_code,
            "name": it.name,
            "qty": float(it.qty),
            "price": float(it.price),
            "discount_pct": float(it.discount_pct),
        }
        for it in item_rows
    ]
    core.event_bus.emit(
        session,
        "leads.lead.converted",
        {
            "lead_id": lead.id,
            "title": lead.product or (lead.message[:60] if lead.message else "") or "Лид",
            "counterparty": lead.company or lead.name or "Новый лид",
            "owner": lead.assigned_to,
            "priority": lead_priority(lead.score),
            "items": items,
            "entity_ref": f"lead:{lead.id}",
        },
    )
    await session.commit()
    return LeadConvertOut(lead_id=lead.id, status=lead.status)


@router.post("/{lead_id}/attachments", response_model=LeadAttachmentOut, status_code=201)
async def upload_attachment(
    lead_id: int,
    payload: LeadAttachmentIn,
    session: AsyncSession = Depends(get_session),
):
    """Загрузить вложение лида (скан заявки, файл письма) — data-URI с клиента.

    Транспорт — тот же паттерн, что и логотип продавца в sales (клиент кодирует
    файл через FileReader, сервер multipart не парсит — его в проекте нет).
    В отличие от логотипа байты не идут в БД: пишутся на диск (``storage.py``,
    граница доверия — тип/размер валидируются там), в БД — только метаданные.
    """
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    try:
        content_type, data = decode_data_url(payload.data_url)
        storage_path, size = save_attachment(lead_id, payload.filename, content_type, data)
    except AttachmentRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    attachment = LeadAttachment(
        lead_id=lead_id,
        filename=payload.filename,
        content_type=content_type,
        size_bytes=size,
        source=payload.source,
        storage_path=storage_path,
    )
    session.add(attachment)
    await session.commit()
    await session.refresh(attachment)
    return attachment


@router.get("/{lead_id}/items", response_model=list[LeadItemOut])
async def list_items(lead_id: int, session: AsyncSession = Depends(get_session)):
    """Позиции подобранного КП лида (корзина каталог-пикера)."""
    query = select(LeadItem).where(LeadItem.lead_id == lead_id).order_by(LeadItem.id)
    return (await session.execute(query)).scalars().all()


@router.put("/{lead_id}/items", response_model=list[LeadItemOut])
async def replace_items(
    lead_id: int,
    payload: list[LeadItemIn],
    session: AsyncSession = Depends(get_session),
):
    """Заменить весь подбор товара лида (replace-all: удалить старые, записать новые).

    Полный список позиций проще всего синхронизировать с корзиной пикера целиком.
    Уже сконвертированный/отклонённый лид — 409 (подбор править нельзя: сделка/счёт
    уже живут своей жизнью, терминальный лид не редактируем)."""
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if lead.status in ("converted", "rejected"):
        raise HTTPException(status_code=409, detail=f"Лид в терминальном статусе: {lead.status}")

    await session.execute(delete(LeadItem).where(LeadItem.lead_id == lead_id))
    rows = [
        LeadItem(
            lead_id=lead_id,
            sku_id=it.sku_id,
            sku_code=it.sku_code,
            name=it.name,
            qty=it.qty,
            price=it.price,
            discount_pct=it.discount_pct,
        )
        for it in payload
    ]
    session.add_all(rows)
    await session.commit()
    query = select(LeadItem).where(LeadItem.lead_id == lead_id).order_by(LeadItem.id)
    return (await session.execute(query)).scalars().all()


@router.post("/{lead_id}/link-contact", response_model=LinkContactOut)
async def link_contact(
    lead_id: int,
    payload: LinkContactIn | None = Body(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Добавить контактное лицо лида в существующую компанию без дублей (Цикл 11).

    Контрагент — из ``payload.counterparty_id`` либо резолва лида (Цикл 10,
    ``lead.counterparty_id``); имя/телефон/e-mail по умолчанию берутся с лида. Делегирует
    ``core.services.mdm.link_contact`` (get-or-create: если контакт с тем же телефоном/e-mail
    уже есть у этой компании — вернём его, не плодя дубль). 422 — если контрагент не определён
    или нет ни одного контактного поля; 404 — если контрагент не существует.
    """
    from core.services import mdm

    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Лид не найден")
    cp_id = (payload.counterparty_id if payload else None) or lead.counterparty_id
    if not cp_id:
        raise HTTPException(
            status_code=422,
            detail="Лид не привязан к компании — сначала резолв клиента или укажите counterparty_id",
        )
    cp = await session.get(Counterparty, cp_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Контрагент не найден")

    full_name = (payload.full_name if payload else "") or lead.name or lead.company
    phone = (payload.phone if payload else None) or lead.phone
    email = (payload.email if payload else None) or lead.email
    if not (full_name.strip() or phone or email):
        raise HTTPException(status_code=422, detail="Нет контактных данных для добавления контакта")

    contact, created = await mdm.link_contact(
        session,
        cp_id,
        full_name=full_name,
        phone=phone,
        email=email,
        is_primary=bool(payload.is_primary) if payload else False,
    )
    await session.commit()
    return LinkContactOut(
        contact_id=contact.id, counterparty_id=cp_id, created=created, full_name=contact.full_name
    )


@router.get("/{lead_id}/attachments", response_model=list[LeadAttachmentOut])
async def list_attachments(lead_id: int, session: AsyncSession = Depends(get_session)):
    """Список вложений лида (без байтов — метаданные; скачать — отдельным эндпоинтом)."""
    query = (
        select(LeadAttachment)
        .where(LeadAttachment.lead_id == lead_id)
        .order_by(LeadAttachment.id.desc())
    )
    return (await session.execute(query)).scalars().all()


@router.get("/{lead_id}/attachments/{attachment_id}/download")
async def download_attachment(
    lead_id: int,
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Скачать/просмотреть байты вложения лида."""
    attachment = await session.get(LeadAttachment, attachment_id)
    if attachment is None or attachment.lead_id != lead_id:
        raise HTTPException(status_code=404, detail="Вложение не найдено")
    try:
        data = read_attachment(attachment.storage_path)
    except (AttachmentRejected, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Файл вложения недоступен на диске") from exc

    # Content-Disposition — только latin-1 (RFC 7230); имя файла может быть кириллицей
    # (тендерная заявка/письмо) — ASCII-фолбэк + RFC 5987 filename* для нормального имени.
    ascii_fallback = attachment.filename.encode("ascii", "ignore").decode("ascii") or "file"
    encoded_name = quote(attachment.filename)
    return Response(
        content=data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_name}'
            )
        },
    )
