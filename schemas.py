"""Pydantic-схемы API модуля Leads (вход/выход), отдельно от ORM-моделей."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modules.leads.leads import LEAD_SOURCES


class LeadCreate(BaseModel):
    """Приём лида из канала (сайт/мессенджер/e-mail/телефония/тендер).

    Границы приёма (ручной POST /leads и API-клиенты): длины полей ограничены под
    колонки БД (иначе 500 на вставке при переполнении String(N)), источник приводится
    к известному каналу, телефон/e-mail нормализуются (обрезка пробелов, e-mail в
    нижний регистр) — грязный ввод из веб-форм/импорта не роняет вставку и не плодит
    «Ivan@x» / «ivan@x » как разные контакты в дедупе."""

    source: str = "site"  # site|telegram|whatsapp|email|phone|tender
    name: str = Field(default="", max_length=255)
    company: str = Field(default="", max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    region: str = Field(default="", max_length=64)
    product: str = Field(default="", max_length=128)
    message: str = Field(default="", max_length=4000)
    # UTM-атрибуция (Цикл 4) — опционально: ручной интейк редко её знает, но API-клиент
    # (например, кампания с прямой публикацией лида) может передать сразу.
    utm_source: str = Field(default="", max_length=128)
    utm_medium: str = Field(default="", max_length=128)
    utm_campaign: str = Field(default="", max_length=128)

    @field_validator("source")
    @classmethod
    def _known_source(cls, v: str) -> str:
        # неизвестный канал → «site» (как в интейке on_intake_lead) — не роняем приём из-за опечатки
        return v if v in LEAD_SOURCES else "site"

    # mode="before" — нормализуем ДО проверки max_length, иначе избыточный паддинг
    # (пробелы) ронял бы длину за лимит ещё до strip (ровно та «гигиена входа», что и цель).
    @field_validator("phone", mode="before")
    @classmethod
    def _clean_phone(cls, v: object) -> str | None:
        if v is None:
            return None
        return str(v).strip() or None

    @field_validator("email", mode="before")
    @classmethod
    def _clean_email(cls, v: object) -> str | None:
        if v is None:
            return None
        return str(v).strip().lower() or None


class LeadOut(BaseModel):
    """Лид в ответах API (вход воронки: приём → квалификация → распределение)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    name: str
    company: str
    phone: str | None = None
    email: str | None = None
    region: str
    product: str
    message: str
    status: str
    score: int
    qualification: str
    reason: str
    assigned_to: str
    funnel: str
    deal_id: int | None = None
    reject_reason: str = ""
    next_step_at: datetime | None = None
    next_step_note: str = ""
    created_at: datetime
    first_action_at: datetime | None = None
    # Подбор товара на лиде (КП): число позиций и сумма (qty*price) — чтобы карточка/drawer
    # показывали «🧾 N поз. · X BYN» без отдельного запроса. Проставляются в роутах (агрегат).
    items_count: int = 0
    items_total: float = 0.0
    # UTM-атрибуция (Цикл 4) — для бейджа кампании на карточке лида
    utm_source: str = ""
    utm_campaign: str = ""
    # Ключевой лид (Цикл 9): высокий потенциал (балл/тендер/объём) — бейдж 🔑 + приоритет
    # маршрутизации к лучшему закрывающему. Производное поле, считается в роутах.
    is_key: bool = False
    # Резолв против существующих клиентов (Цикл 10): эталон контрагента + тип клиента
    # ("" новый | "existing" действующий | "regular" постоянник) — бейдж на карточке.
    counterparty_id: int | None = None
    customer_kind: str = ""
    # Реанимация памяти (Цикл 12): ссылка на ранее отклонённый лид того же контакта → бейдж «был отказ»
    revived_from_id: int | None = None
    # Пост-передача под контролем (Цикл 13): возраст «у продавца» (routed_at) и сторож
    # «converted без сделки» (converted_at + deal_id=NULL дольше пары минут → тревога).
    routed_at: datetime | None = None
    converted_at: datetime | None = None
    # Недозвон + повтор (Цикл 15): попытки контакта, срок перезвона, последнее касание клиента.
    attempt_count: int = 0
    callback_at: datetime | None = None
    last_touch_at: datetime | None = None
    # Рецикл «не сейчас» (Цикл 16): дата авто-возврата в «Новые»; у проснувшегося new-лида
    # поле остаётся в прошлом — фронт рисует бейдж «⏰ проснулся» именно по нему.
    snooze_until: datetime | None = None


class LeadQualifyOut(BaseModel):
    """Результат квалификации лида: балл, вердикт и (опц.) AI-обоснование."""

    id: int
    status: str
    score: int
    qualification: str
    reason: str
    ai_rationale: str | None = None
    model: str | None = None


class LeadRouteOut(BaseModel):
    """Результат распределения лида: назначенный менеджер, тип воронки и обоснование выбора."""

    id: int
    status: str
    assigned_to: str
    funnel: str
    # Цикл 8: почему этот менеджер (конверсия/загрузка или ручной выбор) — нота оператору
    rationale: str = ""


class LeadConvertOut(BaseModel):
    """Результат конвертации лида.

    Сделку создаёт модуль sales по ``leads.lead.converted``; роут ``convert``
    синхронно relay'ит outbox, поэтому ``deal_id`` обычно уже в ответе.
    ``None`` — только если подписчик sales недоступен (тогда догонит фоновый relay).
    """

    lead_id: int
    status: str
    deal_id: int | None = None


class LeadAttachmentIn(BaseModel):
    """Загрузка вложения лида. ``data_url`` — data-URI (``data:<mime>;base64,...``),
    формируется на клиенте через FileReader (сервер multipart не парсит — см. storage.py)."""

    filename: str = Field(min_length=1, max_length=255)
    data_url: str = Field(min_length=1)
    source: str = "manual"  # manual|email|tender


class LeadAttachmentOut(BaseModel):
    """Вложение лида в ответах API (без байтов — только метаданные)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    filename: str
    content_type: str
    size_bytes: int
    source: str
    created_at: datetime


class LeadItemIn(BaseModel):
    """Позиция подбора товара на лиде (тело PUT /leads/{id}/items — полный список, replace-all).

    ``price`` — цена клиенту (уже со скидкой, как в пикере); ``discount_pct`` — скидка справочно.
    """

    sku_id: int
    sku_code: str = ""
    name: str = ""
    # Границы денег: количество строго > 0 (нулевая позиция бессмысленна), цена >= 0,
    # скидка 0..100% — иначе отрицательная цена/скидка исказила бы Σ КП в скорбордах денег.
    qty: float = Field(default=1, gt=0)
    price: float = Field(default=0, ge=0)
    discount_pct: float = Field(default=0, ge=0, le=100)


class LeadItemOut(BaseModel):
    """Позиция подбора товара на лиде в ответах API (GET/PUT /leads/{id}/items)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    lead_id: int
    sku_id: int
    sku_code: str
    name: str
    qty: float
    price: float
    discount_pct: float
    created_at: datetime


class LeadBulkExpressOut(BaseModel):
    """Результат «Разобрать целевых» (Цикл 6): POST /leads/express-bulk.

    ``expressed`` — id распределённых лидов, ``skipped_non_target`` — сколько новых лидов
    пропущено как нецелевые (их лидоруб разбирает вручную, не ошибка)."""

    expressed: list[int]
    skipped_non_target: int


class RouteIn(BaseModel):
    """Опциональное тело POST /route: ручной выбор менеджера вместо авто-правил.

    ``assigned_to`` должен совпадать с одним из известных ``MANAGERS`` (leads.py) —
    иначе 422 (не даём привязать лид к несуществующему/опечатанному имени).
    Без тела (или пустой ``assigned_to``) — прежнее поведение: авто-правила.
    """

    assigned_to: str | None = None
    next_step_at: datetime | None = None
    next_step_note: str | None = None


class LinkContactIn(BaseModel):
    """Тело POST /leads/{id}/link-contact (Цикл 11): добавить контакт в компанию лида.

    Все поля опциональны — по умолчанию берутся с лида (контрагент — из резолва Цикла 10,
    имя/телефон/e-mail — контактные данные лида). ``counterparty_id`` можно переопределить
    (напр. оператор выбрал другую компанию).
    """

    counterparty_id: int | None = None
    full_name: str = ""
    phone: str | None = None
    email: str | None = None
    is_primary: bool = False


class LinkContactOut(BaseModel):
    """Результат привязки контакта: id контакта/контрагента + был ли он создан или уже существовал."""

    contact_id: int
    counterparty_id: int
    created: bool
    full_name: str


class ManagerOut(BaseModel):
    """Менеджер для пикера ручной раздачи: специализация + текущая загрузка."""

    name: str
    regions: list[str]
    products: list[str]
    load: int


class AttemptIn(BaseModel):
    """Опциональное тело POST /attempt (Цикл 15): срок перезвона после недозвона.

    Без тела — дефолт «через 2 часа». С ``callback_at`` — явное обещание клиенту
    («перезвоните в четверг»), которое больше не живёт только в голове лидоруба."""

    callback_at: datetime | None = None


class RejectIn(BaseModel):
    """Тело POST /reject: причина отказа — одна из ``REJECT_REASONS`` (leads.py).

    ``snooze_days`` (Цикл 16) — только для причины «не сейчас»: через сколько дней
    лид сам вернётся в «Новые» (пресеты 30/90/180; без поля — 90)."""

    reason: str = Field(min_length=1)
    snooze_days: int | None = Field(default=None, ge=1, le=365)


class LeadRejectOut(BaseModel):
    """Результат отказа: терминальный статус ``rejected`` + причина."""

    id: int
    status: str
    reject_reason: str


class LeadPlanIn(BaseModel):
    """Тело PUT /leads/plan (Цикл 5): дневные цели лидоруба (норма).

    Все поля >= 0; ``reaction_target_min`` — потолок скорости реакции, остальные — минимум
    за день. Частичное обновление не поддерживаем: панель шлёт полный набор целей.
    """

    leads_target: int = Field(ge=0)
    qualified_target: int = Field(ge=0)
    converted_target: int = Field(ge=0)
    reaction_target_min: int = Field(ge=0)


class LeadPlanOut(BaseModel):
    """План/факт лидоруба за сегодня (Цикл 5): GET/PUT /leads/plan.

    ``*_target`` — дневная норма (leads.lead_plan), ``*_fact`` — факт за сегодня из лидов
    (обработано = первое действие сегодня; целевых передано = из них целевые в routed/
    converted; доведено = converted_at сегодня; reaction_fact_min = ср. скорость реакции,
    None если сегодня ещё не реагировали). Фронт рисует прогресс и «осталось до нормы»."""

    leads_target: int
    qualified_target: int
    converted_target: int
    reaction_target_min: int
    leads_fact: int
    qualified_fact: int
    converted_fact: int
    reaction_fact_min: int | None = None


class LeadSourceStatOut(BaseModel):
    """Отчёт качества источника/кампании (Цикл 4): GET /leads/stats/sources.

    Специалист по лидам видит, какие (источник, кампания) дают целевых лидов и
    конвертируются в сделки, а какие — мусор; маркетинг получает те же цифры для
    решения по бюджету кампании.
    """

    source: str
    utm_campaign: str
    total: int
    target: int
    converted: int
    rejected: int
    avg_score: float
    target_pct: float
    conversion_pct: float
    # Цикл 7: Σ КП сконвертированных лидов источника — «сколько денег отдал продавцам»,
    # чтобы качество источника мерилось деньгами, а не только числом сделок.
    pipeline: float = 0.0


class LeadHandoffStatOut(BaseModel):
    """Скорборд передач лидоруба продавцам (Цикл 7): GET /leads/stats/handoffs.

    Вклад специалиста в план каждого продавца: сколько лидов передал (routed/converted),
    сколько из них продавец довёл до сделки и на какую сумму КП (``pipeline``). Показывает,
    кому лидоруб питает пайплайн деньгами, а не просто числом лидов."""

    manager: str
    assigned: int
    converted: int
    pipeline: float
    conversion_pct: float
    # Цикл 13 — пост-передача под контролем: переданные, но не сконвертированные лиды
    # («в работе» у продавца) с их Σ КП, и сколько из них висят >24ч без сделки.
    pending: int = 0
    pending_pipeline: float = 0.0
    stale: int = 0
