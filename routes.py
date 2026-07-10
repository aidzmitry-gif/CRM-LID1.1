"""HTTP-API модуля Leads. Монтируется ядром под префиксом ``/leads``.

Перенесено из репозитория CRM (бывшие эндпоинты ``/sales/leads*``). Главное
архитектурное отличие после выноса: конвертация не создаёт сделку напрямую —
модуль публикует ``leads.lead.converted``, сделку создаёт модуль sales (§2.4/§2.5).
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import func, select
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
from modules.leads.models import Lead, LeadAttachment
from modules.leads.schemas import (
    LeadAttachmentIn,
    LeadAttachmentOut,
    LeadConvertOut,
    LeadCreate,
    LeadOut,
    LeadQualifyOut,
    LeadRejectOut,
    LeadRouteOut,
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


def _mark_first_action(lead: Lead) -> None:
    """SLA первой реакции: проставить время первого действия лидоруба (один раз)."""
    if lead.first_action_at is None:
        lead.first_action_at = _utcnow()


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
        {"lead_id": lead.id, "source": lead.source, "entity_ref": f"lead:{lead.id}"},
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

    known = await known_customer(session, lead.company)
    score, verdict, reason = score_lead(lead, known)
    lead.score = score
    lead.qualification = verdict
    lead.reason = reason
    if lead.status == "new":
        lead.status = "qualified"
    _mark_first_action(lead)

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

    known = await known_customer(session, lead.company)
    manual_manager = (payload.assigned_to or "").strip() if payload else ""
    if manual_manager:
        if manual_manager not in {m["name"] for m in MANAGERS}:
            raise HTTPException(status_code=422, detail=f"Неизвестный менеджер: {manual_manager}")
        manager, funnel = manual_manager, choose_funnel(lead, known)
    else:
        loads = await _manager_loads(session)
        manager, funnel = route_lead(lead, loads, known)

    lead.assigned_to = manager
    lead.funnel = funnel
    lead.status = "routed"
    _mark_first_action(lead)
    if payload is not None and payload.next_step_at is not None:
        lead.next_step_at = payload.next_step_at
        lead.next_step_note = payload.next_step_note or ""
    core.event_bus.emit(
        session,
        "leads.lead.routed",
        {
            "lead_id": lead.id, "assigned_to": manager, "funnel": funnel,
            "manual": bool(manual_manager), "entity_ref": f"lead:{lead.id}",
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
