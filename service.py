"""普惠托位供给测算服务入口。

python3 service.py --check          配置与存储自检
python3 service.py --port 8000      启动 HTTP 服务（/health 免鉴权）
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from childcare import SERVICE_ID
from childcare.api import ApiApp, make_server
from childcare.domain import SLOT_TYPES, PROVIDER_STATES, POLICY_ACTIONS
from childcare.storage import Store

DB_PATH = os.environ.get("CHILDCARE_DB", "childcare.db")


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


def self_check() -> None:
    """配置与存储自检：枚举可加载、schema 可建、事务可回滚。"""
    with open(os.path.join(os.path.dirname(__file__), "domain.json"),
              encoding="utf-8") as f:
        domain = json.load(f)
    assert domain["托位类型"] == SLOT_TYPES
    assert domain["机构状态"] == PROVIDER_STATES
    assert domain["政策动作"] == POLICY_ACTIONS
    store = Store(":memory:")
    with store.tx() as c:
        c.execute("SELECT 1")
    ApiApp(store)  # 引擎装配
    print("基础检查通过")
    print(f"  托位类型 {len(SLOT_TYPES)} 类、机构状态 {len(PROVIDER_STATES)} 种、"
          f"政策动作 {len(POLICY_ACTIONS)} 项")


# 向后兼容旧测试：保留最小 Handler
class Handler(BaseHTTPRequestHandler):
    """提供运维健康检查（独立运行、无数据库时使用）。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health(), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="普惠托位供给测算")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", default=DB_PATH,
                        help="SQLite 数据库路径（默认读 CHILDCARE_DB 或 childcare.db）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.check:
        self_check()
        return 0

    store = Store(args.db)
    app = ApiApp(store)
    httpd = make_server(args.host, args.port, app)
    print(f"{SERVICE_ID} 监听 http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
