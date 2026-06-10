"""Модуль Leads — реализация ModuleContract (вход воронки CRM).

Перенесено из репозитория CRM: лиды — самостоятельный модуль-плагин со своей
схемой БД и API. С другими модулями общается только событиями шины (§2.4/§2.5):
marketing наполняет приём, sales создаёт сделку по конвертации.
"""
from __future__ import annotations

import logging

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.leads import routes
from modules.leads.events import on_campaign_launched, on_deal_created_from_lead
from modules.leads.permissions import PERMISSIONS, ROLES

logger = logging.getLogger("aios.leads")


class LeadsModule(ModuleContract):
    name = "leads"
    version = "0.1.0"
    api_prefix = "/leads"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        # межмодульные связи через шину (§2.5): модуль не импортирует marketing/sales
        core.subscribe("marketing.campaign.launched", on_campaign_launched)  # кампания → лиды
        core.subscribe("sales.deal.created", on_deal_created_from_lead)  # сделка → ссылка у лида
        core.declare_permissions(PERMISSIONS)
        for role in ROLES:
            core.declare_role(role)
        core.register_widget(Widget("leads_funnel", "Воронка лидов", source="leads.leads"))
        core.on_startup(self._on_startup)

    async def _on_startup(self) -> None:
        logger.info("Leads: модуль готов")


def get_module() -> ModuleContract:
    """Фабрика модуля, вызываемая загрузчиком ядра."""
    return LeadsModule()
