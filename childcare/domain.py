"""领域词汇与机构状态机, 以仓库根目录 domain.json 为准。"""

import json
import os
import re

from .errors import ValidationError

_DOMAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "domain.json")

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def load_domain(path=None):
    """读取领域词汇定义(托位类型、机构状态、政策动作)。"""
    with open(path or _DOMAIN_PATH, encoding="utf-8") as f:
        return json.load(f)


DOMAIN = load_domain()
CLASS_TYPES = tuple(DOMAIN["托位类型"])
STATUSES = tuple(DOMAIN["机构状态"])
ACTIONS = tuple(DOMAIN["政策动作"])

# 机构状态机: 规划→建设→备案→运营, 运营可临时停办(暂停)或退出, 退出为终态。
TRANSITIONS = {
    "规划": {"建设", "退出"},
    "建设": {"备案", "退出"},
    "备案": {"运营", "退出"},
    "运营": {"暂停", "退出"},
    "暂停": {"运营", "退出"},
    "退出": set(),
}

# 场地合规结论
COMPLIANCES = ("待评估", "合格", "不合格")


def check_month(month, label="月份"):
    if not isinstance(month, str) or not MONTH_RE.match(month):
        raise ValidationError(f"{label}须为 YYYY-MM 格式: {month!r}")
    return month


def check_class_type(class_type):
    if class_type not in CLASS_TYPES:
        raise ValidationError(f"未知托位类型: {class_type!r}, 应为 {list(CLASS_TYPES)}")
    return class_type


def check_action(action):
    if action not in ACTIONS:
        raise ValidationError(f"未知政策动作: {action!r}, 应为 {list(ACTIONS)}")
    return action


def check_status(status):
    if status not in STATUSES:
        raise ValidationError(f"未知机构状态: {status!r}, 应为 {list(STATUSES)}")
    return status
