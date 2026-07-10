"""Pydantic-схемы API модуля Leads (вход/выход), отдельно от ORM-моделей."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LeadCreate(BaseModel):
    """Приём лида из канала (сайт/мессенджер/e-mail/телефония/тендер)."""

    source: str = "site"  # site|telegram|whatsapp|email|phone|tender
    name: str = ""
    company: str = ""
    phone: str | None = None
    email: str | None = None
    region: str = ""
    product: str = ""
    message: str = ""


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
    """Результат распределения лида: назначенный менеджер и тип воронки."""

    id: int
    status: str
    assigned_to: str
    funnel: str


class LeadConvertOut(BaseModel):
    """Результат конвертации лида.

    Сделку создаёт модуль sales (репозиторий CRM) по событию
    ``leads.lead.converted``; ``deal_id`` проставляется лиду асинхронно
    обработчиком ответного ``sales.deal.created``.
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
    qty: float = 1
    price: float = 0
    discount_pct: float = 0


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


class RouteIn(BaseModel):
    """Опциональное тело POST /route: ручной выбор менеджера вместо авто-правил.

    ``assigned_to`` должен совпадать с одним из известных ``MANAGERS`` (leads.py) —
    иначе 422 (не даём привязать лид к несуществующему/опечатанному имени).
    Без тела (или пустой ``assigned_to``) — прежнее поведение: авто-правила.
    """

    assigned_to: str | None = None
    next_step_at: datetime | None = None
    next_step_note: str | None = None


class ManagerOut(BaseModel):
    """Менеджер для пикера ручной раздачи: специализация + текущая загрузка."""

    name: str
    regions: list[str]
    products: list[str]
    load: int


class RejectIn(BaseModel):
    """Тело POST /reject: причина отказа — одна из ``REJECT_REASONS`` (leads.py)."""

    reason: str = Field(min_length=1)


class LeadRejectOut(BaseModel):
    """Результат отказа: терминальный статус ``rejected`` + причина."""

    id: int
    status: str
    reject_reason: str
