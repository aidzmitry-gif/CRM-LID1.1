"""Объявляемые модулем Leads роли и разрешения RBAC.

Реальная проверка прав появится в части 5 (Keycloak). Здесь модуль лишь
декларирует, какие права и роли он вводит.
"""
from __future__ import annotations

from core.runtime.contract import Permission, Role

PERMISSIONS = [
    Permission("leads.lead.read", "Просмотр лидов"),
    Permission("leads.lead.write", "Приём и квалификация лидов"),
    Permission("leads.lead.route", "Распределение и конвертация лидов"),
]

ROLES = [
    Role("Менеджер", ("leads.lead.read", "leads.lead.write")),
    Role("РОП", ("leads.lead.read", "leads.lead.write", "leads.lead.route")),
]
