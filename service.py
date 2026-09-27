"""普惠托位供给测算服务入口。"""

import argparse
import json
import tempfile
from http.server import ThreadingHTTPServer

from childcare.api import load_config, make_handler
from childcare.db import init_db
from childcare.domain import load_domain

SERVICE_ID = "inclusive-childcare-supply"


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


def main():
    parser = argparse.ArgumentParser(description="普惠托位供给测算")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        load_domain()
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            init_db(tmp.name)
        print("基础检查通过")
        return
    config = load_config()
    init_db(config["db"])
    print(json.dumps({"service": SERVICE_ID, "db": config["db"],
                      "port": args.port}, ensure_ascii=False))
    ThreadingHTTPServer(("0.0.0.0", args.port),
                        make_handler(config)).serve_forever()


if __name__ == "__main__":
    main()
