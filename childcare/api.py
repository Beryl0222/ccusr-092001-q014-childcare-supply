"""HTTP API 与访问控制。

鉴权：持票访问，服务端持有 token -> (actor, role) 映射，调用方不能自报角色。
数据最小化：
- 候补只接收一次性家庭去重标识（服务端 HMAC 后丢弃原值），任何列表接口
  只返回匿名计数，不返回 anon_key；
- 各角色仅能访问职责范围内的端点（见 ROUTE 表）。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .domain import (
    ROLES,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .engine import FundingEngine, SupplyEngine
from .storage import Store

DEFAULT_TOKENS = {
    "planner-demo-token": ("planner_li", "planner"),
    "funding-demo-token": ("funding_wang", "funding"),
    "intake-demo-token": ("intake_zhao", "intake"),
    "auditor-demo-token": ("auditor_chen", "auditor"),
}

P_FUNDING = {"funding"}
P_PLANNER = {"planner"}
P_INTAKE = {"intake"}
P_AUDITOR = {"auditor"}
P_PLAN_FUND = {"planner", "funding"}
P_VIEW = {"planner", "funding", "auditor"}
P_ALL = {"planner", "funding", "intake", "auditor"}


class ApiApp:
    """持有存储、引擎与鉴权表，被 Handler 调用。"""

    def __init__(self, store: Store, tokens: dict[str, tuple[str, str]] | None = None):
        self.store = store
        self.supply = SupplyEngine(store)
        self.funding = FundingEngine(store)
        self.tokens = tokens or dict(DEFAULT_TOKENS)

    def authenticate(self, auth_header: str | None) -> tuple[str, str] | None:
        if not auth_header:
            return None
        token = auth_header.removeprefix("Bearer ").strip()
        principal = self.tokens.get(token)
        return principal  # (actor, role) 或 None

    def audit(self, actor: str, role: str, action: str, entity: str,
              entity_id: str | None = None, detail: dict | None = None) -> None:
        # 审计明细永不记录家庭去重标识等敏感输入
        self.store.audit(actor, role, action, entity, entity_id, detail)


# 路由：method, 正则, 允许角色, 处理方法名
def _routes() -> list[tuple[str, re.Pattern, set[str], str]]:
    R = []
    def add(method, pattern, roles, fn):
        R.append((method, re.compile("^" + pattern + "$"), roles, fn))

    add("PUT", r"/grids/(?P<grid_id>[\w-]+)", P_PLANNER, "grid_upsert")
    add("PUT", r"/grids/(?P<grid_id>[\w-]+)/demand", P_PLANNER, "demand_upsert")
    add("GET", r"/demand/(?P<month>\d{4}-\d{2})", P_VIEW, "demand_view")

    add("POST", r"/sites", P_PLANNER, "site_create")
    add("GET", r"/sites/(?P<site_id>[\w-]+)", P_VIEW, "site_view")
    add("POST", r"/sites/(?P<site_id>[\w-]+)/service-streets", P_PLANNER,
        "site_service_street")
    add("POST", r"/sites/(?P<site_id>[\w-]+)/phases", P_PLANNER, "phase_create")
    add("PATCH", r"/phases/(?P<phase_id>[\w-]+)/compliance", P_PLANNER,
        "phase_compliance")
    add("PUT", r"/phases/(?P<phase_id>[\w-]+)/schedule", P_PLANNER,
        "phase_schedule")

    add("POST", r"/providers", P_PLANNER, "provider_create")
    add("GET", r"/providers/(?P<provider_id>[\w-]+)", P_VIEW, "provider_view")
    add("POST", r"/providers/(?P<provider_id>[\w-]+)/transition", P_PLAN_FUND,
        "provider_transition")
    add("POST", r"/providers/(?P<provider_id>[\w-]+)/classrooms", P_PLANNER,
        "classroom_create")

    add("GET", r"/classrooms/(?P<classroom_id>[\w-]+)", P_VIEW, "classroom_view")
    add("PATCH", r"/classrooms/(?P<classroom_id>[\w-]+)/capacity", P_PLAN_FUND,
        "capacity_set")
    add("POST", r"/classrooms/(?P<classroom_id>[\w-]+)/stop", P_PLAN_FUND,
        "classroom_stop")
    add("PUT", r"/classrooms/(?P<classroom_id>[\w-]+)/price-commitment",
        P_FUNDING, "price_commit")
    add("PUT", r"/classrooms/(?P<classroom_id>[\w-]+)/price-report",
        P_FUNDING, "price_report")

    add("POST", r"/waitlist", P_INTAKE, "waitlist_register")
    add("GET", r"/streets/(?P<street>[^/]+)/waitlist-summary", P_VIEW,
        "waitlist_summary")

    add("POST", r"/supply/runs", P_PLANNER, "supply_run")
    add("GET", r"/supply/month/(?P<month>\d{4}-\d{2})", P_VIEW, "supply_month")
    add("POST", r"/recommendations", P_PLANNER, "recommendation_create")
    add("GET", r"/recommendations/(?P<rec_id>[\w-]+)", P_VIEW,
        "recommendation_view")

    add("POST", r"/policies", P_FUNDING, "policy_create")
    add("GET", r"/policies", P_VIEW, "policy_list")
    add("GET", r"/policies/(?P<policy_id>[\w-]+)", P_VIEW, "policy_view")

    add("POST", r"/claims", P_FUNDING, "claim_declare")
    add("POST", r"/funding/runs", P_FUNDING, "funding_run")
    add("GET", r"/funding/runs/(?P<run_id>[\w-]+)", P_VIEW, "funding_run_view")
    add("GET", r"/claims/(?P<claim_id>[\w-]+)/explain", P_VIEW, "claim_explain")

    add("GET", r"/audit-log", P_AUDITOR, "audit_view")
    return R


ROUTES = _routes()


class Handler(BaseHTTPRequestHandler):
    server_version = "InclusiveChildcare/1.0"
    app: ApiApp = None  # 由 make_server 注入到类属性

    @property
    def store(self) -> Store:
        return self.app.store

    # ---- 框架 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        from urllib.parse import unquote
        path = unquote(self.path.split("?", 1)[0])
        principal = self.app.authenticate(self.headers.get("Authorization"))
        if principal is None and path != "/health":
            self._error(401, "未提供有效凭证")
            return
        if path == "/health":
            self._json(200, {"status": "ok", "service": "inclusive-childcare-supply"})
            return
        actor, role = principal
        for m, regex, roles, fn_name in ROUTES:
            if m != method:
                continue
            match = regex.match(path)
            if not match:
                continue
            if role not in roles:
                self._error(403, f"角色 {ROLES[role].label} 无权执行此操作")
                return
            body = self._read_body()
            if body is None:
                return
            try:
                getattr(self, fn_name)(actor, role, match.groupdict(), body)
            except ValidationError as exc:
                self._error(400, str(exc))
            except NotFoundError as exc:
                self._error(404, str(exc))
            except ConflictError as exc:
                self._error(409, str(exc))
            return
        self._error(404, f"未知端点：{method} {path}")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体必须为 JSON 对象")
            return None

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def log_message(self, *_args):
        return

    # ---- 字段白名单 ----
    @staticmethod
    def _fields(body: dict, allowed: set[str], required: set[str]) -> dict:
        unknown = set(body) - allowed
        if unknown:
            raise ValidationError(f"存在不允许的字段：{sorted(unknown)}")
        missing = required - set(body)
        if missing:
            raise ValidationError(f"缺少必填字段：{sorted(missing)}")
        return {k: body[k] for k in allowed if k in body}

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    # ========== 需求 ==========
    def grid_upsert(self, actor, role, p, body):
        f = self._fields(body, {"name", "street"}, {"name", "street"})
        self.store.upsert_grid(p["grid_id"], f["name"], f["street"])
        self.app.audit(actor, role, "网格建档", "grid", p["grid_id"],
                       {"street": f["street"]})
        self._json(200, {"grid_id": p["grid_id"]})

    def demand_upsert(self, actor, role, p, body):
        f = self._fields(body, {"month", "slot_type", "children_count",
                                "urgent_count", "source_batch"},
                         {"month", "slot_type", "children_count"})
        self.store.upsert_demand(
            p["grid_id"], f["month"], f["slot_type"], f["children_count"],
            f.get("urgent_count", 0), f.get("source_batch"))
        self.app.audit(actor, role, "聚合需求录入", "demand", p["grid_id"],
                       {"month": f["month"], "slot_type": f["slot_type"],
                        "children": f["children_count"]})
        self._json(200, {"grid_id": p["grid_id"], "month": f["month"]})

    def demand_view(self, actor, role, p, body):
        rows = [self._row(r) for r in self.store.demand_for_month(p["month"])]
        self._json(200, {"month": p["month"], "rows": rows})

    # ========== 场地 / 分期 ==========
    def site_create(self, actor, role, p, body):
        f = self._fields(body, {"name", "street", "address", "note"},
                         {"name", "street"})
        site_id = self.store.create_site(f["name"], f["street"],
                                         f.get("address"), f.get("note"))
        self.app.audit(actor, role, "场地建档", "site", site_id,
                       {"street": f["street"]})
        self._json(201, {"site_id": site_id})

    def site_view(self, actor, role, p, body):
        site = self._row(self.store.get_site(p["site_id"]))
        site["service_streets"] = self.store.service_streets(p["site_id"])
        site["phases"] = [self._row(x) for x in self.store.phases(p["site_id"])]
        self._json(200, site)

    def site_service_street(self, actor, role, p, body):
        f = self._fields(body, {"street"}, {"street"})
        self.store.add_service_street(p["site_id"], f["street"])
        self.app.audit(actor, role, "跨街道服务备案", "site", p["site_id"],
                       {"street": f["street"]})
        self._json(200, {"site_id": p["site_id"], "street": f["street"]})

    def phase_create(self, actor, role, p, body):
        f = self._fields(body, {"seq", "name", "compliance_status",
                                "compliance_capacity", "compliance_ref",
                                "open_month", "close_month"},
                         {"seq", "name", "compliance_status",
                          "compliance_capacity"})
        phase_id = self.store.add_phase(
            p["site_id"], f["seq"], f["name"], f["compliance_status"],
            f["compliance_capacity"], f.get("compliance_ref"),
            f.get("open_month"), f.get("close_month"))
        self.app.audit(actor, role, "场地分期建档", "phase", phase_id,
                       {"site_id": p["site_id"], "seq": f["seq"],
                        "compliance": f["compliance_status"]})
        self._json(201, {"phase_id": phase_id})

    def phase_compliance(self, actor, role, p, body):
        f = self._fields(body, {"compliance_status", "compliance_capacity",
                                "compliance_ref"},
                         {"compliance_status", "compliance_capacity"})
        self.store.update_phase_compliance(
            p["phase_id"], f["compliance_status"], f["compliance_capacity"],
            f.get("compliance_ref"))
        self.app.audit(actor, role, "合规结论变更", "phase", p["phase_id"],
                       {"compliance": f["compliance_status"],
                        "capacity": f["compliance_capacity"]})
        self._json(200, {"phase_id": p["phase_id"]})

    def phase_schedule(self, actor, role, p, body):
        f = self._fields(body, {"open_month", "close_month"}, set())
        self.store.set_phase_schedule(p["phase_id"], f.get("open_month"),
                                      f.get("close_month"))
        self.app.audit(actor, role, "分期排期", "phase", p["phase_id"], f)
        self._json(200, {"phase_id": p["phase_id"], **f})

    # ========== 机构 / 班型 ==========
    def provider_create(self, actor, role, p, body):
        f = self._fields(body, {"name", "license_no", "site_id",
                                "registered_date", "state", "tags"},
                         {"name", "license_no", "site_id", "registered_date"})
        provider_id = self.store.create_provider(
            f["name"], f["license_no"], f["site_id"], f["registered_date"],
            f.get("state", "规划"), f.get("tags"))
        self.app.audit(actor, role, "机构备案", "provider", provider_id,
                       {"license_no": f["license_no"]})
        self._json(201, {"provider_id": provider_id})

    def provider_view(self, actor, role, p, body):
        provider = self._row(self.store.get_provider(p["provider_id"]))
        provider["events"] = [self._row(e)
                              for e in self.store.provider_events(p["provider_id"])]
        self._json(200, provider)

    def provider_transition(self, actor, role, p, body):
        f = self._fields(body, {"to_state", "date", "reason"},
                         {"to_state", "date", "reason"})
        self.store.transition_provider(
            p["provider_id"], f["to_state"], f["date"], f["reason"], actor)
        self.app.audit(actor, role, "机构状态迁移", "provider",
                       p["provider_id"], {"to_state": f["to_state"],
                                          "date": f["date"]})
        self._json(200, {"provider_id": p["provider_id"],
                         "state": f["to_state"]})

    def classroom_create(self, actor, role, p, body):
        f = self._fields(body, {"phase_id", "name", "slot_type",
                                "compliant_capacity", "opened_month",
                                "closed_month"},
                         {"phase_id", "name", "slot_type",
                          "compliant_capacity", "opened_month"})
        classroom_id = self.store.create_classroom(
            p["provider_id"], f["phase_id"], f["name"], f["slot_type"],
            f["compliant_capacity"], f["opened_month"], f.get("closed_month"))
        self.app.audit(actor, role, "班型建档", "classroom", classroom_id,
                       {"slot_type": f["slot_type"],
                        "capacity": f["compliant_capacity"]})
        self._json(201, {"classroom_id": classroom_id, "version": 1})

    def classroom_view(self, actor, role, p, body):
        room = self._row(self.store.get_classroom(p["classroom_id"]))
        self._json(200, room)

    def capacity_set(self, actor, role, p, body):
        f = self._fields(body, {"new_capacity", "expected_version"},
                         {"new_capacity", "expected_version"})
        new_version = self.store.set_capacity(
            p["classroom_id"], f["new_capacity"], f["expected_version"], actor)
        self.app.audit(actor, role, "容量调整", "classroom",
                       p["classroom_id"], {"new_capacity": f["new_capacity"]})
        self._json(200, {"classroom_id": p["classroom_id"],
                         "version": new_version})

    def classroom_stop(self, actor, role, p, body):
        f = self._fields(body, {"close_month"}, {"close_month"})
        self.store.stop_classroom(p["classroom_id"], f["close_month"])
        self.app.audit(actor, role, "班型停办", "classroom",
                       p["classroom_id"], {"close_month": f["close_month"]})
        self._json(200, {"classroom_id": p["classroom_id"], "status": "stopped"})

    def price_commit(self, actor, role, p, body):
        f = self._fields(body, {"monthly_fee_cents", "committed_date"},
                         {"monthly_fee_cents", "committed_date"})
        self.store.commit_price(p["classroom_id"], f["monthly_fee_cents"],
                                f["committed_date"])
        self.app.audit(actor, role, "价格承诺", "classroom",
                       p["classroom_id"], {"fee": f["monthly_fee_cents"]})
        self._json(200, {"classroom_id": p["classroom_id"]})

    def price_report(self, actor, role, p, body):
        f = self._fields(body, {"month", "actual_fee_cents"},
                         {"month", "actual_fee_cents"})
        self.store.report_price(p["classroom_id"], f["month"],
                                f["actual_fee_cents"], actor)
        self.app.audit(actor, role, "实际收费上报", "classroom",
                       p["classroom_id"],
                       {"month": f["month"], "fee": f["actual_fee_cents"]})
        self._json(200, {"classroom_id": p["classroom_id"], "month": f["month"]})

    # ========== 候补（家庭数据最小化） ==========
    def waitlist_register(self, actor, role, p, body):
        # family_token 只在请求内存活，用于 HMAC 后立即丢弃；不落日志
        f = self._fields(body, {"family_token", "street", "slot_type",
                                "target_month", "urgent", "priority_score",
                                "current_month"},
                         {"family_token", "street", "slot_type",
                          "target_month"})
        wid = self.store.upsert_waitlist(
            f["family_token"], f["street"], f["slot_type"], f["target_month"],
            bool(f.get("urgent", False)), f.get("priority_score", 0),
            f.get("current_month", f["target_month"]))
        self.app.audit(actor, role, "候补登记(匿名)", "waitlist", wid,
                       {"street": f["street"], "slot_type": f["slot_type"]})
        # 返回内部受理号，不回显任何可识别家庭的键
        self._json(201, {"receipt_id": wid, "status": "accepted"})

    def waitlist_summary(self, actor, role, p, body):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        month = (qs.get("month") or [None])[0]
        slot_type = (qs.get("slot_type") or [None])[0]
        if month is None:
            raise ValidationError("缺少 month 查询参数")
        rows = self.store.waiting_families(month)
        counts: dict[str, int] = {}
        urgent: dict[str, int] = {}
        for r in rows:
            if r["street"] != p["street"]:
                continue
            if slot_type and r["slot_type"] != slot_type:
                continue
            key = r["slot_type"]
            counts[key] = counts.get(key, 0) + 1
            if r["urgent"]:
                urgent[key] = urgent.get(key, 0) + 1
        self._json(200, {"street": p["street"], "month": month,
                         "waiting_by_type": counts, "urgent_by_type": urgent})

    # ========== 供需测算 ==========
    def supply_run(self, actor, role, p, body):
        f = self._fields(body, {"month", "note", "force"}, {"month"})
        run_id = self.app.supply.run_month(
            f["month"], actor, f.get("note"), force=bool(f.get("force", False)))
        self.app.audit(actor, role, "供需月度测算", "run", run_id,
                       {"month": f["month"], "force": bool(f.get("force"))})
        self._json(201, {"run_id": run_id, "month": f["month"],
                         "snapshot": self.app.supply.monthly_snapshot(f["month"])})

    def supply_month(self, actor, role, p, body):
        self._json(200, self.app.supply.monthly_snapshot(p["month"]))

    def recommendation_create(self, actor, role, p, body):
        f = self._fields(body, {"street", "slot_type", "months",
                                "proposed_capacity", "site_id", "phase_id",
                                "rationale"},
                         {"street", "slot_type", "months",
                          "proposed_capacity"})
        rec_id = self.app.supply.recommend(
            f["street"], f["slot_type"], f["months"], f["proposed_capacity"],
            actor, site_id=f.get("site_id"), phase_id=f.get("phase_id"),
            rationale=f.get("rationale"))
        self.app.audit(actor, role, "建设建议", "recommendation", rec_id,
                       {"street": f["street"], "slot_type": f["slot_type"],
                        "capacity": f["proposed_capacity"]})
        self._json(201, self.app.supply.explain_recommendation(rec_id))

    def recommendation_view(self, actor, role, p, body):
        self._json(200, self.app.supply.explain_recommendation(p["rec_id"]))

    # ========== 政策 / 资金 ==========
    def policy_create(self, actor, role, p, body):
        f = self._fields(body, {"version_no", "effective_month", "rules",
                                "retroactive", "supersedes_policy_id", "note"},
                         {"version_no", "effective_month", "rules"})
        policy_id = self.store.create_policy(
            f["version_no"], f["effective_month"], f["rules"],
            retroactive=bool(f.get("retroactive", False)),
            supersedes_policy_id=f.get("supersedes_policy_id"),
            note=f.get("note"))
        self.app.audit(actor, role, "政策版本发布", "policy", policy_id,
                       {"version_no": f["version_no"],
                        "effective_month": f["effective_month"],
                        "retroactive": bool(f.get("retroactive"))})
        self._json(201, {"policy_id": policy_id,
                         "version_no": f["version_no"]})

    def policy_list(self, actor, role, p, body):
        self._json(200, {"policies": [self._row(x)
                                      for x in self.store.policies()]})

    def policy_view(self, actor, role, p, body):
        policy = self._row(self.store.get_policy(p["policy_id"]))
        policy["rules"] = [self._row(r)
                           for r in self.store.policy_rules(p["policy_id"])]
        self._json(200, policy)

    def claim_declare(self, actor, role, p, body):
        f = self._fields(body, {"provider_id", "action", "month",
                                "classroom_id", "declared_amount_cents"},
                         {"provider_id", "action", "month",
                          "declared_amount_cents"})
        claim_id = self.store.declare_claim(
            f["provider_id"], f["action"], f["month"],
            f.get("classroom_id"), f["declared_amount_cents"], actor)
        self.app.audit(actor, role, "补助申报受理", "claim", claim_id,
                       {"action": f["action"], "month": f["month"]})
        self._json(201, {"claim_id": claim_id, "status": "submitted"})

    def funding_run(self, actor, role, p, body):
        f = self._fields(body, {"month", "policy_id", "note"}, {"month"})
        result = self.app.funding.run_month(
            f["month"], actor, policy_id=f.get("policy_id"), note=f.get("note"))
        self.app.audit(actor, role, "资金月度测算", "run", result["run_id"],
                       {"month": f["month"],
                        "policy_version": result["policy_version_no"],
                        "new_claims": len(result["created"]),
                        "supplements": len(result["supplements"]),
                        "clawbacks": len(result["clawbacks"]),
                        "voided": len(result["voided"])})
        self._json(201, result)

    def funding_run_view(self, actor, role, p, body):
        self._json(200, self.app.funding.run_summary(p["run_id"]))

    def claim_explain(self, actor, role, p, body):
        self._json(200, self.app.funding.explain_claim(p["claim_id"]))

    # ========== 审计 ==========
    def audit_view(self, actor, role, p, body):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        limit = int((qs.get("limit") or ["100"])[0])
        with self.store.tx() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (min(limit, 1000),)).fetchall()]
        self._json(200, {"entries": rows})


def make_server(host: str, port: int, app: ApiApp,
                server_cls=ThreadingHTTPServer) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = server_cls((host, port), handler)
    return httpd
