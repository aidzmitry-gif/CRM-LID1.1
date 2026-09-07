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
    from modules.leads.leads import (
        apply_initial_score,
        cancel_pending_wake,
        find_last_rejected_by_contact,
        find_open_lead_by_phone,
        phone_tail,
    )
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
        # Фикс ревью Ц15: в событии звонка нет company — если открытых лидов с этим
        # хвостом несколько у РАЗНЫХ компаний (общий коммутатор), дубль неоднозначен:
        # не гадаем и не пишем «не тому» (тихий no-op, как до Цикла 15).
        same_tail = (
            await ctx.session.execute(
                select(Lead).where(
                    Lead.status.in_(("new", "qualified", "routed")),
                    Lead.phone.isnot(None),
                    Lead.phone.like(f"%{tail}"),
                )
            )
        ).scalars().all()
        companies = {c.company.strip().lower() for c in same_tail if (c.company or "").strip()}
        if len(companies) > 1:
            logger.info("Leads: повторный звонок с %s неоднозначен (компаний: %d) — пропуск", phone, len(companies))
            return
        # Цикл 15: повторный звонок — самый горячий сигнал покупки, раньше исчезал
        # бесследно (голый return). Теперь след в message + метка касания → бейдж
        # «↑ повтор» и подъём лида на доске.
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        note = f"Повторный звонок ({stamp}, доб. {payload.get('agent_ext') or '—'})"
        dup.message = f"{dup.message}\n---\n{note}" if dup.message else note
        dup.last_touch_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.info("Leads: повторный звонок с %s → отмечен на лиде %s", phone, dup.id)
        return

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
    # Тот же контакт мог получить отказ «не сейчас» и спать — гасим авто-возврат, чтобы
    # звонок не породил фантомный дубль (спящий лид + этот). Память об отказе — revived_from_id.
    prior_rej = await find_last_rejected_by_contact(ctx.session, phone, None)
    if prior_rej is not None:
        lead.revived_from_id = prior_rej.id
        cancel_pending_wake(prior_rej)
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
    if "receipt_id" in payload:
        if ctx is None:
            raise RuntimeError("Receipt delivery requires a transactional context")
        await _on_intake_receipt(payload["receipt_id"], ctx)
        return
    if ctx is None:
        return
    from modules.leads.leads import (
        LEAD_SOURCES,
        apply_initial_score,
        cancel_pending_wake,
        find_last_rejected_by_contact,
        find_open_lead_by_email,
        find_open_lead_by_phone,
        resolve_customer,
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
    company = (payload.get("company") or "").strip()

    # company (Цикл 12): тот же телефон/e-mail у другой компании — не дубль, заводим отдельный лид
    dup = await find_open_lead_by_phone(ctx.session, phone, company)
    if dup is None:
        dup = await find_open_lead_by_email(ctx.session, email, company)
    if dup is not None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        note = f"Повторное обращение ({source}, {stamp}): {message}".strip()
        dup.message = f"{dup.message}\n---\n{note}" if dup.message else note
        dup.last_touch_at = datetime.now(timezone.utc).replace(tzinfo=None)  # Цикл 15: «↑ повтор»
        await apply_initial_score(dup, ctx.session)  # пересчёт балла с учётом нового обращения
        # Цикл 10 (фикс ревью): если лид ещё холодный — повторно резолвим против клиентов.
        # Контрагент/контакт могли появиться в MDM ПОСЛЕ создания лида; иначе действующий
        # клиент навсегда остался бы «холодным» на повторных обращениях (ранний выход).
        if not dup.customer_kind:
            await resolve_customer(ctx.session, dup)
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
    await resolve_customer(ctx.session, lead)  # Цикл 10: резолв против существующих клиентов
    prior_rej = await find_last_rejected_by_contact(ctx.session, phone, email, company)
    if prior_rej is not None:
        lead.revived_from_id = prior_rej.id  # Цикл 12: память об отказе
        cancel_pending_wake(prior_rej)  # контакт вернулся сам → спящий «не сейчас» не будит дубль
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


async def on_intake_receipt_check(payload: dict, ctx) -> None:
    """Read-only query through the core bus; no emits or domain state changes.

    Fail closed unless every promised row still belongs to the existing lead and
    every promised byte is present. Share locks keep checked rows stable for the
    request transaction on PostgreSQL; a caller must start with verified=False.
    """
    from sqlalchemy import select

    from core.domain.models import IntakeIdentity, IntakeReceipt
    from core.services import intake_storage
    from modules.leads.models import Lead, LeadAttachment

    payload["verified"] = False
    if ctx is None:
        return
    receipt = await ctx.session.get(IntakeReceipt, payload.get("receipt_id"), populate_existing=True)
    if receipt is None or receipt.status != "delivered":
        return
    identity = await ctx.session.get(IntakeIdentity, receipt.identity_id, populate_existing=True)
    if identity is None or identity.lead_id is None:
        return
    lead_id = await ctx.session.scalar(select(Lead.id).where(
        Lead.id == identity.lead_id,
    ).with_for_update(read=True))
    if lead_id is None:
        return
    expected = receipt.payload.get("files", [])
    if len(expected) != len(receipt.files):
        return
    expected_by_id = {item["file_id"]: item for item in expected}
    attachment_ids = [item.get("attachment_id") for item in receipt.files]
    if any(value is None for value in attachment_ids) or len(set(attachment_ids)) != len(attachment_ids):
        return
    rows = (await ctx.session.scalars(select(LeadAttachment).where(
        LeadAttachment.id.in_(attachment_ids), LeadAttachment.lead_id == lead_id,
    ).with_for_update(read=True).execution_options(populate_existing=True))).all()
    if len(rows) != len(attachment_ids):
        return
    by_id = {row.id: row for row in rows}
    for item in receipt.files:
        promised = expected_by_id.get(item["file_id"])
        if promised is None or any(item.get(key) != value for key, value in promised.items()):
            return
        row = by_id[item["attachment_id"]]
        if any(getattr(row, key) != item[key] for key in (
            "filename", "content_type", "size_bytes", "storage_path",
        )):
            return
        intake_storage.verify_file(intake_storage.attachment_root(), item)
    payload["verified"] = True


async def _intake_manages_customer(session, lead) -> bool:
    """Only replace a binding demonstrably left by the previous intake delivery.

    The existing lead schema has no manual/auto flag. Record our result in the
    transactional domain event, separate from the immutable input receipt. A
    different current value, an absent marker or an already external binding is
    preserved. A manual write of exactly the same values is not observable in
    this schema; there is currently no manual binding setter in the leads API.
    """
    from sqlalchemy import select

    from core.domain.models import OutboxEvent

    event = (await session.execute(select(OutboxEvent).where(
        OutboxEvent.event_type.in_(("leads.lead.received", "leads.lead.intake_updated")),
        OutboxEvent.payload["lead_id"].as_integer() == lead.id,
        OutboxEvent.payload["receipt_id"].as_string().is_not(None),
    ).order_by(OutboxEvent.id.desc()).limit(1))).scalar_one_or_none()
    binding = event.payload.get("customer_binding", {}) if event else {}
    return bool(
        binding.get("managed") is True
        and binding.get("counterparty_id") == lead.counterparty_id
        and binding.get("customer_kind") == lead.customer_kind
    )


async def _on_intake_receipt(receipt_id: str, ctx) -> None:
    """Process one inbox entry, isolating its failure from unrelated relay events.

    No commits here: the relay commits the lead, attachments, receipt and domain
    event together. A failed savepoint leaves a durable failed receipt for retry
    by the authenticated producer, without marking it as delivered.
    """
    from sqlalchemy import select, update

    from core.domain.models import IntakeIdentity, IntakeReceipt
    from core.services import intake_storage
    from modules.leads.leads import apply_initial_score, resolve_customer
    from modules.leads.models import Lead, LeadAttachment

    session = ctx.session
    receipt = await session.get(IntakeReceipt, receipt_id)
    if receipt is None:
        # An orphan event must not poison the global relay; it cannot yield a receipt.
        logger.error("Intake event references missing receipt %s", receipt_id)
        return
    identity = (await session.execute(select(IntakeIdentity).where(
        IntakeIdentity.id == receipt.identity_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    receipt = (await session.execute(select(IntakeReceipt).where(
        IntakeReceipt.id == receipt_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    if receipt.status != "queued":
        return
    if session.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE and may otherwise release the first SAVEPOINT
        # as a commit. Begin its write transaction before the per-receipt savepoint.
        await session.execute(update(IntakeReceipt).where(IntakeReceipt.id == receipt_id).values(
            updated_at=IntakeReceipt.updated_at,
        ))
    try:
        async with session.begin_nested():
            for item in receipt.files:
                intake_storage.verify_file(intake_storage.attachment_root(), item)
            data = receipt.payload
            fields = dict(data["lead"])
            fields.pop("landing_url", None)  # Attribution belongs to the domain event.
            text = fields.pop("message", "")
            provenance = (
                f"Источник: {receipt.namespace}; ID: {identity.source_id}; "
                f"доставка: {receipt.delivery_id}"
            )
            message = "\n".join(filter(None, (
                provenance, data.get("source_url"), data.get("subject"), text,
            )))
            source = {
                "admin@enersys.by": "email", "zakupki.legat.by": "tender",
            }.get(identity.namespace, "site")
            # Ordinary lead editors do not acquire the intake identity lock.
            # Refresh and lock their row before comparing our binding evidence.
            lead = await session.get(
                Lead, identity.lead_id, with_for_update=True, populate_existing=True,
            ) if identity.lead_id else None
            if identity.lead_id and lead is None:
                raise ValueError("The linked lead no longer exists")
            created = lead is None
            managed_binding = created or await _intake_manages_customer(session, lead)
            previous_contacts = (lead.phone, lead.email, lead.company) if lead else None
            if created:
                lead = Lead(**fields, source=source, message=message, status="new")
                session.add(lead)
                await session.flush()
                identity.lead_id = lead.id
            else:
                lead.message = f"{lead.message}\n---\n{message}" if lead.message else message
                lead.last_touch_at = datetime.now(timezone.utc).replace(tzinfo=None)
                for key, value in fields.items():
                    # A mail copy can arrive first; the direct site record owns
                    # its contact fields. Copies only fill fields still missing.
                    if value and (receipt.namespace == identity.namespace or not getattr(lead, key)):
                        setattr(lead, key, value)
            await apply_initial_score(lead, session)
            authoritative_change = (
                receipt.namespace == identity.namespace
                and previous_contacts != (lead.phone, lead.email, lead.company)
            )
            if managed_binding and (created or authoritative_change or not lead.customer_kind):
                # resolve_customer returns without clearing on no match. Clear
                # only a proven automatic result; external/manual choices survive.
                lead.counterparty_id, lead.customer_kind = None, ""
                await resolve_customer(session, lead)
            manifest = []
            for item in receipt.files:
                attachment = LeadAttachment(
                    lead_id=lead.id, filename=item["filename"], content_type=item["content_type"],
                    size_bytes=item["size_bytes"], storage_path=item["storage_path"],
                    source="tender" if source == "tender" else "email" if source == "email" else "site",
                )
                session.add(attachment)
                await session.flush()
                manifest.append({**item, "attachment_id": attachment.id})
            receipt.files = manifest
            receipt.status, receipt.error_code = "delivered", None
            receipt.delivered_at = datetime.now(timezone.utc).replace(tzinfo=None)
            receipt.updated_at = receipt.delivered_at
            ctx.services.event_bus.emit(session, (
                "leads.lead.received" if created else "leads.lead.intake_updated"
            ), {
                "lead_id": lead.id, "source": source, "entity_ref": f"lead:{lead.id}",
                "receipt_id": receipt.id,
                "customer_binding": {
                    "managed": managed_binding,
                    "counterparty_id": lead.counterparty_id,
                    "customer_kind": lead.customer_kind,
                },
                **{key: data["lead"].get(key, "") for key in (
                    "utm_source", "utm_medium", "utm_campaign", "landing_url",
                )},
            })
            await session.flush()
    except Exception as exc:
        # Do not catch cancellation; DB/lead writes were rolled back to the
        # savepoint. Expired ORM state must be explicitly reloaded after rollback.
        receipt = await session.get(IntakeReceipt, receipt_id, populate_existing=True)
        receipt.status = "failed"
        receipt.error_code = (
            "attachment_unavailable" if isinstance(exc, (OSError, intake_storage.AttachmentRejected))
            else "processing_failed"
        )
        receipt.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.warning("Intake receipt %s failed: %s", receipt_id, type(exc).__name__)


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
