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
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.leads.ai import qualify_lead
from modules.leads.leads import (
    MANAGERS,
    REJECT_REASONS,
    apply_initial_score,
    choose_funnel,
    find_open_lead_by_email,
    find_open_lead_by_phone,
    known_customer,
    lead_priority,
    route_lead,
    score_lead,
)
from modules.leads.models import Lead, LeadAttachment, LeadItem
from modules.leads.schemas import (
    LeadAttachmentIn,
    LeadAttachmentOut,
    LeadConvertOut,
    LeadCreate,
    LeadItemIn,
    LeadItemOut,
    LeadOut,
    LeadQualifyOut,
    LeadRejectOut,
    LeadRouteOut,
    LeadSourceStatOut,
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


async def _resolve_manager(lead: Lead, session: AsyncSession, manual_manager: str) -> tuple[str, str]:
    """Выбрать менеджера и воронку — вручную (с проверкой по ``MANAGERS``) либо по авто-правилам.

    ``manual_manager`` должен совпадать с одним из известных ``MANAGERS``, иначе 422 (не
    даём привязать лид к несуществующему/опечатанному имени). Общая логика для /route и
    /express (Цикл 2) — проверка живёт в одном месте.
    """
    known = await known_customer(session, lead.company)
    if manual_manager:
        if manual_manager not in {m["name"] for m in MANAGERS}:
            raise HTTPException(status_code=422, detail=f"Неизвестный менеджер: {manual_manager}")
        return manual_manager, choose_funnel(lead, known)
    loads = await _manager_loads(session)
    return route_lead(lead, loads, known)


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
    dup = await find_open_lead_by_phone(session, payload.phone)
    if dup is None:
        dup = await find_open_lead_by_email(session, payload.email)
    if dup is not None:
        raise HTTPException(
            status_code=409,
            detail={"duplicate_of": dup.id, "message": f"Дубль лида #{dup.id}"},
        )

    lead = Lead(**payload.model_dump())
    session.add(lead)
    await apply_initial_score(lead, session)  # балл сразу на входе, статус остаётся new
    await session.flush()
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
    target = func.sum(case((Lead.qualification == "target", 1), else_=0))
    converted = func.sum(case((Lead.status == "converted", 1), else_=0))
    rejected = func.sum(case((Lead.status == "rejected", 1), else_=0))
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
            )
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
        )
        for source, utm_campaign, n, n_target, n_converted, n_rejected, avg_score in rows
    ]


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
    manager, funnel = await _resolve_manager(lead, session, manual_manager)
    _apply_route(
        lead, manager, funnel,
        payload.next_step_at if payload else None,
        payload.next_step_note if payload else None,
    )
    _emit_routed(lead, manager, funnel, bool(manual_manager), core, session)
    await session.commit()
    return LeadRouteOut(id=lead.id, status=lead.status, assigned_to=manager, funnel=funnel)


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
    manager, funnel = await _resolve_manager(lead, session, manual_manager)
    _apply_route(
        lead, manager, funnel,
        payload.next_step_at if payload else None,
        payload.next_step_note if payload else None,
    )
    _emit_routed(lead, manager, funnel, bool(manual_manager), core, session)

    await session.commit()
    (await _attach_item_totals(session, [lead]))
    return lead


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
