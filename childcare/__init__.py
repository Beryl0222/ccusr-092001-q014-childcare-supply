"""普惠托位供给测算后端。

仅依赖标准库（sqlite3），按月输出可追溯的供需与资金测算。
家庭侧只保存聚合需求与不可逆哈希后的候补标识，不落任何身份明细。
"""

from .domain import (
    SLOT_TYPES,
    PROVIDER_STATES,
    POLICY_ACTIONS,
    CAPACITY_ACTIONS,
    VALID_TRANSITIONS,
    month_add,
    month_between,
    current_month,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .storage import Store
from .engine import SupplyEngine, FundingEngine

SERVICE_ID = "inclusive-childcare-supply"

__all__ = [
    "SLOT_TYPES",
    "PROVIDER_STATES",
    "POLICY_ACTIONS",
    "CAPACITY_ACTIONS",
    "VALID_TRANSITIONS",
    "month_add",
    "month_between",
    "current_month",
    "ConflictError",
    "NotFoundError",
    "ValidationError",
    "Store",
    "SupplyEngine",
    "FundingEngine",
    "SERVICE_ID",
]
