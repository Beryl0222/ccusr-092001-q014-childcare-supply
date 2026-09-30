"""领域枚举与基础规则，取值以 domain.json 为准。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_HERE, "domain.json"), encoding="utf-8") as _f:
    _DOMAIN = json.load(_f)

SLOT_TYPES: list[str] = list(_DOMAIN["托位类型"])
PROVIDER_STATES: list[str] = list(_DOMAIN["机构状态"])
POLICY_ACTIONS: list[str] = list(_DOMAIN["政策动作"])

# 补助资金测算覆盖的政策动作（“补助追回”是结果动作，不做正向计提）。
CAPACITY_ACTIONS: list[str] = [a for a in POLICY_ACTIONS if a != "补助追回"]

# 机构状态机（“同一场地分阶段启用”与“临时停办”都通过该状态机表达）。
VALID_TRANSITIONS: dict[str, set[str]] = {
    "规划": {"建设", "退出"},
    "建设": {"备案", "退出"},
    "备案": {"运营", "退出"},
    "运营": {"暂停", "退出"},
    "暂停": {"运营", "退出"},
    "退出": set(),
}

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class ValidationError(ValueError):
    """请求数据不满足领域约束。"""


class NotFoundError(LookupError):
    """引用的实体不存在。"""


class ConflictError(RuntimeError):
    """并发或容量约束冲突（如已分配托位超过合规上限）。"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def check_month(value: str) -> str:
    if not isinstance(value, str) or not MONTH_RE.match(value):
        raise ValidationError(f"月份格式应为 YYYY-MM：{value!r}")
    return value


def check_date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value!r}")
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:  # 例如 2026-02-30
        raise ValidationError(str(exc)) from exc
    return value


def month_of(date_iso: str) -> str:
    check_date(date_iso)
    return date_iso[:7]


def current_month() -> str:
    return _dt.date.today().strftime("%Y-%m")


def month_add(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_between(start_inclusive: str, end_inclusive: str) -> list[str]:
    """返回闭区间内的月份列表；start>end 时为空。"""
    a = int(start_inclusive[:4]) * 12 + int(start_inclusive[5:7]) - 1
    b = int(end_inclusive[:4]) * 12 + int(end_inclusive[5:7]) - 1
    return [month_add(start_inclusive, i) for i in range(b - a + 1)]


@dataclass(frozen=True)
class Role:
    """服务端角色：决定可见端点与可见字段。"""

    name: str
    label: str


ROLES = {
    "planner": Role("planner", "区域服务规划人员"),
    "funding": Role("funding", "卫生健康资金管理人员"),
    "intake": Role("intake", "街道受理人员"),
    "auditor": Role("auditor", "审计监督人员"),
}
