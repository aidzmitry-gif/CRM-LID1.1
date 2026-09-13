"""ORM-модели модуля Leads (собственная схема ``leads.*``)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class LeadAttachment(Base):
    """Вложение лида (скан тендерной заявки, файл из письма, ручная загрузка).

    Метаданные — здесь; сами байты — на диске (``modules/leads/storage.py``,
    ``save_attachment``/``read_attachment``), НЕ в БД: сканы/xlsx весят до
    нескольких МБ, раздувать Postgres-строки не нужно (в отличие от логотипа
    продавца в sales — тот маленький, хранится как data-URI прямо в колонке).
    """

    __tablename__ = "lead_attachment"
    __table_args__ = {"schema": "leads"}

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.lead.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column()
    # ручная загрузка спецом | email-вложение | скан с тендерной площадки
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    storage_path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LeadItem(Base):
    """Позиция подбора товара на лиде — корзина каталог-пикера, сохранённая ДО сделки.

    Спец по лидам подбирает товар прямо во время общения с клиентом (тот же
    каталог-пикер, что и в сделках), подбор оседает здесь как КП. При конвертации
    «В сделку + счёт» позиции переносятся в ``sales.deal_item`` фронтом (addDealItem)
    и уходят в счёт — сама таблица живёт только на стороне лида.

    ``sku_id`` — мягкая ссылка на shared-kernel ``Sku`` (как ``sales.deal_item``, без
    cross-schema FK); ``sku_code``/``name`` продублированы, чтобы показать КП без join.
    ``price`` — цена клиенту (уже с учётом скидки, как ``priceOverride`` пикера);
    ``discount_pct`` — сама скидка справочно (сумма КП считается как ``qty*price``)."""

    __tablename__ = "lead_item"
    __table_args__ = {"schema": "leads"}

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.lead.id", ondelete="CASCADE"))
    sku_id: Mapped[int] = mapped_column()
    sku_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("1"), server_default="1")
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LeadPlan(Base):
    """Дневная норма лидоруба (Цикл 5) — план по обработке лидов, к которому меряется факт.

    Одна «вечнозелёная» строка на период (``period='daily'``): цели НЕ обнуляются к новому
    дню — это норма, а факт считается за сегодня из самих лидов (routes.py ``_plan_facts``).
    Правит цели РОП/лидоруб (PUT /leads/plan). ``reaction_target_min`` — «держать НЕ выше»
    (потолок скорости первой реакции), остальные три — «набрать НЕ ниже» за день.
    """

    __tablename__ = "lead_plan"
    __table_args__ = {"schema": "leads"}

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(16), unique=True, default="daily", server_default="daily")
    leads_target: Mapped[int] = mapped_column(default=20, server_default="20")  # обработать лидов/день
    qualified_target: Mapped[int] = mapped_column(default=8, server_default="8")  # целевых передать/день
    converted_target: Mapped[int] = mapped_column(default=3, server_default="3")  # довести до сделки/день
    reaction_target_min: Mapped[int] = mapped_column(default=15, server_default="15")  # потолок реакции, мин
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Lead(Base):
    """Лид — вход воронки CRM (приём → квалификация → распределение → сделка).

    Front-of-funnel из ФАЗЫ 1: входящие заявки из каналов (сайт, мессенджеры,
    e-mail, телефония, тендеры) собираются здесь до превращения в сделку.
    ``score``/``qualification`` заполняет квалификатор (эвристики + AI-обоснование,
    §2.5), ``assigned_to``/``funnel`` — движок распределения (правила: география,
    продукт, нагрузка, тип воронки). Сделку по событию ``leads.lead.converted``
    создаёт модуль sales (репозиторий CRM); ``deal_id`` проставляется обработчиком
    ответного события ``sales.deal.created``, ``status`` = ``converted``.
    """

    __tablename__ = "lead"
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_lead_owner_request"), {"schema": "leads"})

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(index=True)
    crm_client_id: Mapped[int | None] = mapped_column(index=True)
    crm_contact_id: Mapped[int | None] = mapped_column(index=True)
    request_key: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="site", server_default="site")
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    company: Mapped[str] = mapped_column(String(255), default="", server_default="")
    phone: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(64), default="", server_default="")
    product: Mapped[str] = mapped_column(String(128), default="", server_default="")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    # new → qualified → routed → converted (или rejected при отказе)
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    score: Mapped[int] = mapped_column(default=0, server_default="0")
    qualification: Mapped[str] = mapped_column(String(16), default="", server_default="")
    reason: Mapped[str] = mapped_column(String(255), default="", server_default="")
    assigned_to: Mapped[str] = mapped_column(String(128), default="", server_default="")
    funnel: Mapped[str] = mapped_column(String(16), default="", server_default="")
    deal_id: Mapped[int | None] = mapped_column()
    # причина отказа (см. REJECT_REASONS в leads.py) — заполняется POST /reject
    reject_reason: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # срок+заметка для продавца — выставляются при раздаче (POST /route)
    next_step_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_step_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # SLA первой реакции: время первого действия лидоруба (qualify/route/reject), NULL пока не тронут
    first_action_at: Mapped[datetime | None] = mapped_column(DateTime)
    # момент конвертации в сделку (POST /convert) — для дневного план/факта (Цикл 5) и
    # петли исхода по времени (Цикл 7); NULL пока лид не сконвертирован
    converted_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Момент передачи продавцу (/route, /express, /express-bulk) — пост-передача под
    # контролем (Цикл 13): возраст «у продавца» на карточке, подсветка зависших >24ч
    # в скорборде передач; NULL пока лид не распределён (у старых routed-лидов тоже NULL).
    routed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Недозвон как состояние (Цикл 15): счётчик попыток контакта и срок перезвона.
    # 93% конверсий достигаются к 6-й попытке — недозвонённый лид не теряется, а живёт
    # в очереди «перезвонить к…» (просроченный callback подсвечивается и всплывает).
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    callback_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Повторное касание клиента (Цикл 15): дубль-звонок/повторная заявка обновляют метку —
    # бейдж «↑ повтор» и подъём лида (повтор — самый горячий сигнал покупки дня).
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Рецикл «не сейчас» (Цикл 16): отказ с созревающим спросом откладывается до даты —
    # при её наступлении лид сам возвращается в «Новые» (бейдж «⏰ проснулся»), а не
    # теряется навсегда: 77% некупивших сразу покупают в течение 2 лет.
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime)
    # UTM-атрибуция (Цикл 4): источник/канал/кампания рекламы, приведшей лид — для отчёта
    # качества источников (routes.py) и атрибуции marketing (leads.lead.received → Campaign)
    utm_source: Mapped[str] = mapped_column(String(128), default="", server_default="")
    utm_medium: Mapped[str] = mapped_column(String(128), default="", server_default="")
    utm_campaign: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # Резолв против существующих клиентов (Цикл 10): мягкая ссылка на эталон контрагента
    # (public.counterparty, без cross-schema FK — как sku_id) и тип клиента:
    # "" новый | "existing" действующий клиент из MDM/1С | "regular" постоянник (были лиды)
    counterparty_id: Mapped[int | None] = mapped_column()
    customer_kind: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # Реанимация памяти (Цикл 12): ссылка на ранее отклонённый лид того же контакта —
    # продавец видит, что с этим контактом уже был отказ (и его причину), а не работает вслепую.
    revived_from_id: Mapped[int | None] = mapped_column()
