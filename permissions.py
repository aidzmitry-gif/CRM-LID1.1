"""Объявляемые модулем Leads роли и разрешения RBAC.

Права навешаны на роуты (``routes.py``, ``require_permission``, fail-closed через
``core.services.auth``): аноним/чужой отдел получают 403 на вход воронки (ПДн клиентов,
конвертация, вложения). ``has_permission`` сопоставляет ``role.name`` с ролями пользователя,
а те приходят РЕАЛЬНЫМИ слагами (``config/access.py``: ``sales_head``/``sales``/``sales_cli``,
не «Менеджер»/«РОП») — поэтому роли объявляем под слаги, иначе право получал бы только
суперюзер (Директор/Коммерческий — минуют проверку). Суперроли доступ имеют всегда.
Полноценный role-mapping — с Keycloak (часть 5, SECURITY.md)."""
from __future__ import annotations

from core.runtime.contract import Permission, Role

PERMISSIONS = [
    Permission("leads.lead.read", "Просмотр лидов"),
    Permission("leads.lead.write", "Приём и квалификация лидов"),
    Permission("leads.lead.route", "Распределение и конвертация лидов"),
]

# Слаги ролей — из config/access.py. РОП и продавцы ведут воронку целиком (приём →
# квалификация → распределение → конвертация); клиентская работа (sales_cli) — приём/
# квалификация без раздачи. Директор/Коммерческий — суперроли (полный доступ минуя это).
ROLES = [
    Role("sales_head", ("leads.lead.read", "leads.lead.write", "leads.lead.route")),
    Role("sales", ("leads.lead.read", "leads.lead.write", "leads.lead.route")),
    Role("sales_cli", ("leads.lead.read", "leads.lead.write")),
]
