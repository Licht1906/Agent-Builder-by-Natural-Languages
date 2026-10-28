"""`python -m src.standalone --config /etc/agent/runtime.yaml` — start the exported package (DS-13).

The order is the point. `runtime.yaml` and its secrets are read first, with
nothing but `src.standalone.config` imported, because the package's own
Postgres URL is one of those secrets and `src.db.base` binds its engine the
moment it is imported. Only once `DATABASE_URL` names the package's database
are the modules that touch it imported.

Any reason not to start — a missing secret, a bundle that does not load, a
Hệ CSDL that cannot be reached — is printed as one sentence and exits with
status 2, before a port is opened.
"""

import argparse
import os
import sys
from pathlib import Path

from src.standalone.config import RuntimeConfigError, load_runtime_config, resolve_secrets, secret_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.standalone", description="Chạy gói agent đã xuất")
    parser.add_argument("--config", default=os.environ.get("AGENT_RUNTIME_CONFIG", "runtime.yaml"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    try:
        config = load_runtime_config(config_path)
        secrets = resolve_secrets(config, secret_source(config.secrets, base=config_path.parent))
    except RuntimeConfigError as exc:
        print(f"Gói không khởi động: {exc}", file=sys.stderr)
        return 2

    os.environ["DATABASE_URL"] = secrets[config.database.url_secret]
    from src.config import get_settings

    get_settings.cache_clear()

    import uvicorn

    from src.standalone.app import create_app
    from src.standalone.documents import prepare_knowledge
    from src.standalone.package import load_package

    try:
        package = load_package(config, secrets, base=config_path.parent)
        # Before the port opens: an answer must never cite a shelf still half-indexed (DS-18).
        knowledge, report = prepare_knowledge(config, package.shelves, secrets, base=config_path.parent)
        app = create_app(package, knowledge=knowledge)
    except RuntimeConfigError as exc:
        print(f"Gói không khởi động: {exc}", file=sys.stderr)
        return 2
    if package.shelves:
        print(f"Tài liệu: {len(report.indexed)} file vừa index, {len(report.kept)} file đã có sẵn", file=sys.stderr)
        for label, reason in report.failed.items():
            print(f"Không index được {label}: {reason}", file=sys.stderr)
    # The loop the async Postgres driver talks to — see `src.server_loop`.
    uvicorn.run(app, host=args.host, port=args.port, loop="src.server_loop:new_event_loop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
