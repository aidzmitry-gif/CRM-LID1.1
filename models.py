"""ORM-модели модуля Leads (собственная схема ``leads.*``)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


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
    __table_args__ = {"schema": "leads"}

    id: Mapped[int] = mapped_column(primary_key=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
