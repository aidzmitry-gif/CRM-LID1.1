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
