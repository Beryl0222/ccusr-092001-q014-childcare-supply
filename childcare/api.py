"""HTTP 接口层: 角色鉴权、字段最小化校验与路由。

角色与职责(任何人员不得看到不属于职责范围的明细):
  planner  规划人员: 需求/场地/阶段上报, 触发测算, 查看缺口与建设建议
  health   卫健部门: 合规结论、机构备案与状态、服务范围、容量、价格、分配、候补登记
  finance  财政部门: 政策版本发布, 补助确认与追回, 查看资金台账
  provider 机构:     仅本机构的容量申报与价格承诺
  auditor  审计:     只读缺口、资金、建议与审计日志
家庭数据仅以 HMAC 伪名入库, 对外只暴露聚合计数, 任何角色都拿不到明细。
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import calc, store
from .db import connect, query, transact
from .errors import ApiError, Forbidden, Unauthorized, ValidationError
from .security import pseudonym

DEFAULT_TOKENS = {
    "dev-planner": {"role": "planner"},
    "dev-health": {"role": "health"},
    "dev-finance": {"role": "finance"},
    "dev-auditor": {"role": "auditor"},
}

ROLES = ("planner", "health", "finance", "provider", "auditor")


def load_config():
    """从环境变量装配运行配置。"""
    tokens = os.environ.get("CHILDCARE_TOKENS")
    return {
        "db": os.environ.get("CHILDCARE_DB", "childcare.db"),
        "secret": os.environ.get("CHILDCARE_SECRET", "dev-secret").encode("utf-8"),
        "tokens": json.loads(tokens) if tokens else dict(DEFAULT_TOKENS),
    }


def _check_keys(body, required, optional=()):
    """严格字段校验: 拒绝职责范围外的字段(数据最小化)。"""
    if not isinstance(body, dict):
        raise ValidationError("请求体须为 JSON 对象")
    allowed = set(required) | set(optional)
    extra = set(body) - allowed
    if extra:
        raise ValidationError(f"包含不允许的字段: {sorted(extra)}")
    missing = [k for k in required if k not in body]
    if missing:
        raise ValidationError(f"缺少必填字段: {missing}")


# ---------------------------------------------------------------- 各端点处理

def _h_demand(app, auth, m, body, q):
    _check_keys(body, ["records"])
    records = body["records"]
    if not isinstance(records, list) or not records:
        raise ValidationError("records 须为非空数组")
    def work(conn):
        for rec in records:
            _check_keys(rec, ["grid", "street", "month", "class_type", "demand"])
            store.upsert_demand(conn, rec["grid"], rec["street"], rec["month"],
                                rec["class_type"], rec["demand"],
                                actor=auth["role"])
        return {"accepted": len(records)}
    return 201, transact(app["db"], work)


def _h_create_site(app, auth, m, body, q):
    _check_keys(body, ["site_id", "street", "kind"])
    def work(conn):
        store.create_site(conn, body["site_id"], body["street"], body["kind"],
                          actor=auth["role"])
        return {"site_id": body["site_id"]}
    return 201, transact(app["db"], work)


def _h_compliance(app, auth, m, body, q):
    _check_keys(body, ["compliance"], ["note"])
    sid = m.group("sid")
    def work(conn):
        store.set_compliance(conn, sid, body["compliance"],
                             body.get("note", ""), actor=auth["role"])
        return {"site_id": sid, "compliance": body["compliance"]}
    return 200, transact(app["db"], work)


def _h_phase(app, auth, m, body, q):
    _check_keys(body, ["phase", "start_month", "class_type", "capacity"],
                ["end_month"])
    sid = m.group("sid")
    def work(conn):
        store.upsert_phase(conn, sid, body["phase"], body["start_month"],
                           body.get("end_month"), body["class_type"],
                           body["capacity"], actor=auth["role"])
        return {"site_id": sid, "phase": body["phase"]}
    return 200, transact(app["db"], work)


def _h_register_provider(app, auth, m, body, q):
    _check_keys(body, ["provider_id", "site_id", "name", "month"], ["model"])
    def work(conn):
        store.register_provider(conn, body["provider_id"], body["site_id"],
                                body["name"], body["month"],
                                bool(body.get("model")), actor=auth["role"])
        return {"provider_id": body["provider_id"]}
    return 201, transact(app["db"], work)


def _h_status(app, auth, m, body, q):
    _check_keys(body, ["to_status", "month"], ["end_month", "reason"])
    pid = m.group("pid")
    def work(conn):
        store.change_status(conn, pid, body["to_status"], body["month"],
                            body.get("end_month"), body.get("reason", ""),
                            actor=auth["role"])
        return {"provider_id": pid, "status": body["to_status"]}
    return 200, transact(app["db"], work)


def _h_service_areas(app, auth, m, body, q):
    _check_keys(body, ["areas"])
    pid = m.group("pid")
    def work(conn):
        store.set_service_areas(conn, pid, body["areas"], actor=auth["role"])
        return {"provider_id": pid, "areas": len(body["areas"])}
    return 200, transact(app["db"], work)


def _provider_self(auth, pid):
    """机构角色只能操作本机构。"""
    if auth["role"] == "provider" and auth.get("provider_id") != pid:
        raise Forbidden("机构只能操作本机构的数据")


def _h_capacity(app, auth, m, body, q):
    _check_keys(body, ["class_type", "month", "capacity"])
    pid = m.group("pid")
    _provider_self(auth, pid)
    def work(conn):
        store.set_capacity(conn, pid, body["class_type"], body["month"],
                           body["capacity"], actor=auth["role"])
        return {"provider_id": pid}
    return 200, transact(app["db"], work)


def _h_price(app, auth, m, body, q):
    _check_keys(body, ["class_type", "month", "price"])
    pid = m.group("pid")
    _provider_self(auth, pid)
    def work(conn):
        store.add_price(conn, pid, body["class_type"], body["month"],
                        body["price"], actor=auth["role"])
        return {"provider_id": pid}
    return 201, transact(app["db"], work)


def _h_allocate(app, auth, m, body, q):
    _check_keys(body, ["month", "class_type", "count"])
    pid = m.group("pid")
    def work(conn):
        total = store.allocate(conn, pid, body["month"], body["class_type"],
                               body["count"], actor=auth["role"])
        return {"provider_id": pid, "allocated": total}
    return 200, transact(app["db"], work)


def _h_waitlist(app, auth, m, body, q):
    # 仅接受测算所需字段; family_ref 立即散列为伪名, 原文不留存
    _check_keys(body, ["month", "class_type", "grid", "street", "family_ref"])
    pseudo = pseudonym(app["secret"], body["family_ref"])
    def work(conn):
        store.add_waitlist(conn, body["month"], body["class_type"],
                           body["grid"], body["street"], pseudo,
                           actor=auth["role"])
        return {"accepted": True}
    return 201, transact(app["db"], work)


def _h_waitlist_summary(app, auth, m, body, q):
    month = q.get("month", [None])[0]
    street = q.get("street", [None])[0]
    return 200, {"lines": query(app["db"], store.waitlist_summary,
                                month, street)}


def _h_add_policy(app, auth, m, body, q):
    _check_keys(body, ["version", "action", "params", "applies_from",
                       "published_month"], ["class_type"])
    def work(conn):
        pid = store.add_policy(conn, body["version"], body["action"],
                               body.get("class_type", ""), body["params"],
                               body["applies_from"], body["published_month"],
                               actor=auth["role"])
        return {"policy_id": pid}
    return 201, transact(app["db"], work)


def _h_list_policies(app, auth, m, body, q):
    action = q.get("action", [None])[0]
    return 200, {"lines": query(app["db"], store.list_policies, action)}


def _h_calculate(app, auth, m, body, q):
    _check_keys(body or {}, [], ["note"])
    month = m.group("month")
    result = transact(app["db"], calc.calculate_month, month,
                      (body or {}).get("note", ""), auth["role"])
    return 201, result


def _h_gaps(app, auth, m, body, q):
    return 200, query(app["db"], calc.gaps, m.group("month"))


def _h_funding(app, auth, m, body, q):
    return 200, query(app["db"], calc.funding, m.group("month"))


def _h_confirm(app, auth, m, body, q):
    sid = int(m.group("sid"))
    return 200, transact(app["db"], calc.confirm_subsidy, sid, auth["role"])


def _h_clawback(app, auth, m, body, q):
    _check_keys(body, ["amount", "reason"])
    sid = int(m.group("sid"))
    def work(conn):
        new_id = calc.clawback(conn, sid, body["amount"], body["reason"],
                               actor=auth["role"])
        return {"clawback_id": new_id}
    return 201, transact(app["db"], work)


def _h_explain_subsidy(app, auth, m, body, q):
    return 200, query(app["db"], calc.explain_subsidy, int(m.group("sid")))


def _h_recommendations(app, auth, m, body, q):
    month = q.get("month", [None])[0]
    if not month:
        raise ValidationError("缺少查询参数 month")
    return 200, query(app["db"], calc.recommendations, month)


def _h_explain_rec(app, auth, m, body, q):
    return 200, query(app["db"], calc.explain_recommendation,
                      int(m.group("rid")))


def _h_audit(app, auth, m, body, q):
    return 200, {"lines": query(app["db"], calc.list_audit)}


def _h_health(app, auth, m, body, q):
    return 200, {"status": "ok", "service": "inclusive-childcare-supply"}


# (方法, 路径, 允许角色(None=公开), 处理器)
ROUTES = [
    ("GET",  re.compile(r"^/health$"), None, _h_health),
    ("POST", re.compile(r"^/demand$"), {"planner", "health"}, _h_demand),
    ("POST", re.compile(r"^/sites$"), {"planner"}, _h_create_site),
    ("PUT",  re.compile(r"^/sites/(?P<sid>[^/]+)/compliance$"),
     {"health"}, _h_compliance),
    ("PUT",  re.compile(r"^/sites/(?P<sid>[^/]+)/phases$"),
     {"planner"}, _h_phase),
    ("POST", re.compile(r"^/providers$"), {"health"}, _h_register_provider),
    ("POST", re.compile(r"^/providers/(?P<pid>[^/]+)/status$"),
     {"health"}, _h_status),
    ("PUT",  re.compile(r"^/providers/(?P<pid>[^/]+)/service-areas$"),
     {"health"}, _h_service_areas),
    ("PUT",  re.compile(r"^/providers/(?P<pid>[^/]+)/capacity$"),
     {"health", "provider"}, _h_capacity),
    ("POST", re.compile(r"^/providers/(?P<pid>[^/]+)/prices$"),
     {"health", "provider"}, _h_price),
    ("POST", re.compile(r"^/providers/(?P<pid>[^/]+)/allocations$"),
     {"health"}, _h_allocate),
    ("POST", re.compile(r"^/waitlist$"), {"health", "provider"}, _h_waitlist),
    ("GET",  re.compile(r"^/waitlist/summary$"),
     {"planner", "health", "auditor"}, _h_waitlist_summary),
    ("POST", re.compile(r"^/policies$"), {"finance"}, _h_add_policy),
    ("GET",  re.compile(r"^/policies$"),
     {"finance", "health", "planner", "auditor"}, _h_list_policies),
    ("POST", re.compile(r"^/months/(?P<month>\d{4}-\d{2})/calculate$"),
     {"planner"}, _h_calculate),
    ("GET",  re.compile(r"^/months/(?P<month>\d{4}-\d{2})/gaps$"),
     {"planner", "health", "auditor"}, _h_gaps),
    ("GET",  re.compile(r"^/months/(?P<month>\d{4}-\d{2})/funding$"),
     {"finance", "health", "auditor"}, _h_funding),
    ("POST", re.compile(r"^/subsidies/(?P<sid>\d+)/confirm$"),
     {"finance"}, _h_confirm),
    ("POST", re.compile(r"^/subsidies/(?P<sid>\d+)/clawback$"),
     {"finance"}, _h_clawback),
    ("GET",  re.compile(r"^/subsidies/(?P<sid>\d+)/explain$"),
     {"finance", "health", "auditor"}, _h_explain_subsidy),
    ("GET",  re.compile(r"^/recommendations$"),
     {"planner", "health", "auditor"}, _h_recommendations),
    ("GET",  re.compile(r"^/recommendations/(?P<rid>\d+)/explain$"),
     {"planner", "health", "auditor"}, _h_explain_rec),
    ("GET",  re.compile(r"^/audit-log$"), {"auditor"}, _h_audit),
]


def make_handler(app):
    """构造绑定指定应用配置的请求处理器。"""

    class ApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # -------------------------------------------------- 基础工具
        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return None
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValidationError("请求体不是合法 JSON")

        def _auth(self):
            token = self.headers.get("X-Token", "")
            auth = app["tokens"].get(token)
            if auth is None:
                raise Unauthorized("缺少或无效的访问令牌")
            if auth.get("role") not in ROLES:
                raise Unauthorized("令牌角色无效")
            return auth

        # -------------------------------------------------- 分发
        def _dispatch(self, method):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query_args = parse_qs(parsed.query)
                for mth, pattern, roles, handler in ROUTES:
                    if mth != method:
                        continue
                    match = pattern.match(path)
                    if not match:
                        continue
                    auth = None
                    if roles is not None:
                        auth = self._auth()
                        if auth["role"] not in roles:
                            raise Forbidden(
                                f"角色 {auth['role']} 无权访问该资源")
                    body = self._read_json() if method in ("POST", "PUT") \
                        else None
                    status, obj = handler(app, auth, match, body, query_args)
                    self._send(status, obj)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except ApiError as e:
                self._send(e.status, {"error": e.code, "message": e.message})
            except Exception as e:  # 兜底, 不外泄内部细节
                self._send(500, {"error": "internal",
                                 "message": f"内部错误: {type(e).__name__}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def log_message(self, *_args):
            return

    return ApiHandler
