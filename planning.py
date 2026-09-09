"""Обратная воронка планирования лидов — чистый расчёт (без I/O).

Спека: ``leads-planning-plan.md``. Считаем СВЕРХУ ВНИЗ от цели по выручке (деньги — цель #1)
к требуемому числу сырых лидов, плюс сверка СНИЗУ ВВЕРХ (физическая ёмкость лидоруба) и
покрытие воронки (требуемая Σ сделок в работе). Здесь ТОЛЬКО арифметика — таблицы/эндпоинты/
события лежат в ``models.py``/``routes.py``/``events.py`` и портируются отдельными слайсами
(эндпоинт ``/crm/leads/planning/reverse`` зовёт ``reverse_funnel``, чтобы фронт не хардкодил формулу).

Честность (деньги #1): битый коэффициент (0, >1, отрицательный чек) — это ValueError, а НЕ
``inf``/деление на ноль. План, посчитанный на мусоре, опаснее отсутствия плана.

Округление: число лидов на каждом уровне — ``ceil`` от точного (нужно НЕ МЕНЬШЕ N; 0.45 лида
округляем вверх). Промежуточные уровни считаем от ТОЧНЫХ float (без накопления ошибки),
``ceil`` — только на отчётном целом. Деньги — ``Decimal`` (без float-дрейфа копеек).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

# Порог наращивания бюджета канала: доход к затратам на привлечение (LTV/CAC).
LTV_CAC_MIN = 3.0


def _rate(name: str, value: float) -> float:
    """Коэффициент воронки: обязан быть в (0, 1]. Иначе — честная ошибка, не inf/крэш."""
    v = float(value)
    if not (0.0 < v <= 1.0):
        raise ValueError(f"{name}: коэффициент воронки должен быть в (0, 1], получено {value!r}")
    return v


def _money(name: str, value) -> Decimal:
    """Сумма BYN как Decimal; отрицательная — ошибка (не бывает отрицательной выручки/чека)."""
    d = Decimal(str(value))
    if d < 0:
        raise ValueError(f"{name}: сумма не может быть отрицательной, получено {value!r}")
    return d


@dataclass(frozen=True)
class ReverseFunnel:
    """Результат обратной воронки: уровни сверху вниз + требуемые сырые лиды + покрытие."""

    revenue_target_byn: Decimal
    avg_deal_byn: Decimal
    deals_needed: int          # сделок нужно
    sql_needed: int            # готовых лидов нужно (квалифицированных, переданных закрывающему)
    mql_needed: int            # тёплых обращений нужно
    raw_needed: int            # сырых лидов нужно
    raw_per_workday: int       # сырых лидов в рабочий день
    pipeline_needed_byn: Decimal  # требуемая Σ сделок в работе = цель / доля сделок
    pipeline_multiple: float      # запас воронки (раз) = 1 / доля сделок


def reverse_funnel(
    *,
    revenue_target_byn,
    avg_deal_byn,
    win_rate: float,       # доля сделок: готовый лид → сделка
    conv_mql_sql: float,   # конверсия: тёплое обращение → готовый лид
    conv_raw_mql: float,   # конверсия: сырой → тёплое обращение (порог авто-скоринга)
    workdays: int = 26,
) -> ReverseFunnel:
    """Реверс «цель по выручке → сырые лиды». Все коэффициенты — доли в (0, 1].

    workdays — рабочих дней в месяце (вс выходной, суббота рабочая; дефолт 26).
    """
    target = _money("revenue_target_byn", revenue_target_byn)
    avg = _money("avg_deal_byn", avg_deal_byn)
    if avg <= 0:
        raise ValueError("avg_deal_byn: средний чек должен быть > 0")
    if workdays <= 0:
        raise ValueError(f"workdays: рабочих дней должно быть > 0, получено {workdays!r}")
    wr = _rate("win_rate", win_rate)
    c_ms = _rate("conv_mql_sql", conv_mql_sql)
    c_rm = _rate("conv_raw_mql", conv_raw_mql)

    # Уровни считаем от ТОЧНЫХ float (ceil — только на отчётном целом, без накопления ошибки).
    deals = float(target / avg)
    sql = deals / wr
    mql = sql / c_ms
    raw = mql / c_rm

    pipeline = (target / Decimal(str(wr))).quantize(Decimal("0.01"))
    return ReverseFunnel(
        revenue_target_byn=target,
        avg_deal_byn=avg,
        deals_needed=math.ceil(deals),
        sql_needed=math.ceil(sql),
        mql_needed=math.ceil(mql),
        raw_needed=math.ceil(raw),
        raw_per_workday=math.ceil(raw / workdays),
        pipeline_needed_byn=pipeline,
        pipeline_multiple=round(1.0 / wr, 2),
    )


@dataclass(frozen=True)
class CapacityCheck:
    """Сверка снизу вверх: физическая ёмкость лидоруба против потребности сверху вниз."""

    capacity_per_month: int
    required_raw: int
    delta: int          # потребность − ёмкость; > 0 = дефицит (не хватает рук)
    is_deficit: bool


def capacity_check(
    *,
    required_raw: int,
    capacity_per_day: float,
    workdays: int = 26,
    ramp_factor: float = 1.0,
) -> CapacityCheck:
    """Ёмкость/мес = сырых/день × рабочих дней × коэффициент разгона новичка (floor — честно вниз).

    ramp_factor — доля производительности новичка в первые 4-8 недель (1.0 = полная).
    Дельта > 0 подсвечивает разрыв: экран предлагает РОПу (а) поднять качество источников,
    (б) второго лидоруба, (в) скорректировать план продаж вниз (спека §Ёмкость).
    """
    if required_raw < 0:
        raise ValueError("required_raw: не может быть отрицательным")
    if capacity_per_day < 0:
        raise ValueError("capacity_per_day: не может быть отрицательной")
    if workdays <= 0:
        raise ValueError(f"workdays: должно быть > 0, получено {workdays!r}")
    if not (0.0 < ramp_factor <= 1.0):
        raise ValueError(f"ramp_factor: коэффициент разгона в (0, 1], получено {ramp_factor!r}")
    cap = math.floor(capacity_per_day * workdays * ramp_factor)
    delta = required_raw - cap
    return CapacityCheck(
        capacity_per_month=cap,
        required_raw=required_raw,
        delta=delta,
        is_deficit=delta > 0,
    )
