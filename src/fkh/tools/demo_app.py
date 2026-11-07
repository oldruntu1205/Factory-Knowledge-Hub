"""本地私域演示环境：一条命令起「内嵌 PG + API + 演示页」。

它模拟的是客户内网形态，不是公网服务：
- 数据库：内嵌 pgserver（数据落在 `--pgdata`，默认 `.demo-pgdata`）
- API：uvicorn 只绑 127.0.0.1（演示页服务端代持 JWT，浏览器里不出现令牌）
- 演示页：内置 http.server，按需代理 `/api/answer`、`/api/status`

为什么演示页不挂进 FastAPI：它不是对外契约，挂进去会让 OpenAPI 契约测试和面变大。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jwt
import psycopg

from fkh.tools.conflict_page import CONFLICTS_PAGE
from fkh.tools.demo_page import PAGE
from fkh.tools.documents_page import DOCUMENTS_PAGE
from fkh.tools.evidence_client import EVIDENCE_JS
from fkh.tools.experience_page import EXPERIENCE_PAGE
from fkh.tools.training_page import TRAINING_PAGE

DEMO_TENANT_NAME = "三爱思演示"

_TRAINING_TEXT = """冷凝管安装与检验（培训材料）

一、安装
将冷凝管竖直固定在铁架台上，下端进水口接自来水，上端出水口接排水管。
水流方向必须与蒸汽方向相反，否则冷凝效率显著下降。

二、气密性检验
两端封堵后充气至 0.2MPa，保压 60 秒；压力下降不超过 5% 为合格。
不合格品转入返修区，返修后须重做全部三项检验。

三、安全
通水后再加热，停热后继续通水 5 分钟再关水；避免骤冷骤热导致玻璃炸裂。
"""


def _pg_bin() -> Path:
    """复用随包安装的 PG 16 + pgvector 二进制。"""
    import pgserver

    return Path(pgserver.__file__).parent / "pginstall" / "bin"


def _run(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[Any]:
    """跑 PG 二进制。

    `pg_ctl start` **不能**用管道抓输出：它启动的 postgres 子进程会继承 stdout，
    父进程读不到 EOF 就一直等下去（实测卡死 4 分钟）。启动这类命令一律用 DEVNULL。
    """
    if not capture:
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ensure_postgres(pgdata: Path, port: int) -> str:
    """确保内嵌 PG 在跑，返回 DSN。

    不用 pgserver.get_server：它内部 `pg_ctl -w` 只等 10 秒，而本机 HDD 上一次非正常关机后的
    fsync 恢复可能 30 秒以上，会被误判成启动失败（实测踩过两次）。
    """
    pgdata.mkdir(parents=True, exist_ok=True)
    bin_dir = _pg_bin()

    if not (pgdata / "PG_VERSION").exists():
        created = _run(
            [
                str(bin_dir / "initdb.exe"),
                "-D",
                str(pgdata),
                "-U",
                "postgres",
                "-A",
                "trust",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
        if created.returncode != 0:
            raise SystemExit(f"initdb 失败：\n{created.stdout}\n{created.stderr}")

    status = _run([str(bin_dir / "pg_ctl.exe"), "-D", str(pgdata), "status"])
    if status.returncode != 0:
        started = _run(
            [
                str(bin_dir / "pg_ctl.exe"),
                "-D",
                str(pgdata),
                "-o",
                f"-p {port} -h 127.0.0.1",
                "-t",
                "180",
                "-l",
                str(pgdata / "log"),
                "start",
            ],
            capture=False,
        )
        if started.returncode != 0:
            log = (pgdata / "log").read_text(encoding="utf-8", errors="replace")[-2000:]
            raise SystemExit(f"PostgreSQL 启动失败：\n{started.stdout}\n{started.stderr}\n{log}")
    return f"postgresql://postgres:@127.0.0.1:{port}/postgres"


def stop_postgres(pgdata: Path) -> None:
    bin_dir = _pg_bin()
    _run([str(bin_dir / "pg_ctl.exe"), "-D", str(pgdata), "-m", "fast", "-w", "-t", "120", "stop"])


def _apply_migrations(dsn: str) -> None:
    from migrations.apply import main as apply_main

    apply_main(["--url", dsn])


def _seed(dsn: str, corpus: Path, username: str) -> dict[str, Any]:
    """幂等播种：租户/主体/语料/审批/目录抽取。重复启动不会重复入库。"""
    from fkh.authz.policy import AclPolicy
    from fkh.domain.review import ReviewService
    from fkh.workers.ingestion import IngestionRequest, ingest_document

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT id FROM tenants WHERE name = %s", (DEMO_TENANT_NAME,)
        ).fetchone()
        if row is None:
            created = conn.execute(
                "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (DEMO_TENANT_NAME,)
            ).fetchone()
            assert created is not None
            tenant = created[0]
            conn.commit()
        else:
            tenant = row[0]
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))

        principal = conn.execute(
            "SELECT id FROM principals WHERE tenant_id = %s AND external_subject = %s",
            (tenant, f"{username}@demo.local"),
        ).fetchone()
        if principal is None:
            created = conn.execute(
                """INSERT INTO principals (tenant_id, external_subject, principal_type)
                   VALUES (%s, %s, 'user') RETURNING id""",
                (tenant, f"{username}@demo.local"),
            ).fetchone()
            assert created is not None
            principal = created[0]
            conn.commit()
        principal = principal[0] if isinstance(principal, tuple) else principal

        public_principal = conn.execute(
            "SELECT id FROM principals WHERE tenant_id = %s AND external_subject = %s",
            (tenant, "publisher@demo.local"),
        ).fetchone()
        if public_principal is None:
            created = conn.execute(
                """INSERT INTO principals (tenant_id, external_subject, principal_type)
                   VALUES (%s, %s, 'user') RETURNING id""",
                (tenant, "publisher@demo.local"),
            ).fetchone()
            assert created is not None
            public_principal = created[0]
            conn.commit()
        public_principal = (
            public_principal[0] if isinstance(public_principal, tuple) else public_principal
        )

        chunks = int(
            _scalar(conn, "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s", (tenant,))
        )
        if chunks == 0:
            result = ingest_document(
                conn,
                uuid.UUID(str(tenant)),
                IngestionRequest(
                    source_id="demo-catalog-2023",
                    filename=corpus.name,
                    content=corpus.read_bytes(),
                    knowledge_key="demo-catalog-2023",
                    title="三爱思 2023 产品目录",
                    domain="product_catalog",
                    owner_department="sales",
                    classification=2,
                    audience="internal",
                    required_scope="internal_product",
                    acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
                    actor_id=uuid.UUID(str(principal)),
                    catalog_currency="CNY",
                ),
            )
            review = ReviewService(conn, uuid.UUID(str(tenant)))
            review.pass_review(result.version_id, uuid.UUID(str(principal)))
            review.approve_internal(result.version_id, uuid.UUID(str(principal)))
            conn.commit()

        # 培训链路演示数据：文件 → 课程 → 分配给演示主体（幂等）
        from fkh.domain.training import TrainingService

        training = TrainingService(conn, uuid.UUID(str(tenant)))
        existing_course = conn.execute(
            "SELECT course_id FROM training_courses WHERE tenant_id = %s AND title = %s",
            (tenant, "冷凝管安装与检验（演示）"),
        ).fetchone()
        if existing_course is None:
            content = _TRAINING_TEXT.encode()
            doc = conn.execute(
                "SELECT id FROM knowledge_documents WHERE tenant_id = %s AND knowledge_key = %s",
                (tenant, "demo-training-qual"),
            ).fetchone()
            if doc is None:
                result = ingest_document(
                    conn,
                    uuid.UUID(str(tenant)),
                    IngestionRequest(
                        source_id="demo-training-qual",
                        filename="training.md",
                        content=content,
                        knowledge_key="demo-training-qual",
                        title="冷凝管安装与检验（培训）",
                        domain="training",
                        owner_department="hr",
                        classification=1,
                        audience="internal",
                        required_scope="internal_training",
                        acl_policy=AclPolicy.from_json({"allow": {"roles": ["staff"]}, "deny": {}}),
                        actor_id=uuid.UUID(str(principal)),
                    ),
                )
                review = ReviewService(conn, uuid.UUID(str(tenant)))
                review.pass_review(result.version_id, uuid.UUID(str(principal)))
                review.approve_internal(result.version_id, uuid.UUID(str(principal)))
                conn.commit()
                version_id = result.version_id
            else:
                latest = conn.execute(
                    "SELECT id FROM knowledge_versions WHERE tenant_id = %s AND document_id = %s"
                    " ORDER BY version_no DESC LIMIT 1",
                    (tenant, doc[0]),
                ).fetchone()
                assert latest is not None
                version_id = latest[0]
            role_id = training.ensure_role("installer", "安装工")
            course_id = training.create_course(role_id=role_id, title="冷凝管安装与检验（演示）")
            training.add_source(course_id, version_id)
            training.create_question(
                course_id=course_id,
                course_version=1,
                question_type="SINGLE_CHOICE",
                prompt="气密性测试的保压时间是多少？",
                answer_key={"correct": "60 秒"},
                evidence_refs=[],
                status="APPROVED",
            )
            training.publish_course(course_id)
            # 学员身份取**令牌 sub**（与 _issue_token 的构造一致），否则学员看不到任务
            training.assign(
                course_id=course_id,
                learner_subject_id=f"{str(principal)[:8]}@demo.local",
            )
            conn.commit()

        stats = {
            "tenant": str(tenant),
            "principal": str(principal),
            "subject": f"{str(principal)[:8]}@demo.local",
            "public_principal": str(public_principal),
            "chunks": int(
                _scalar(
                    conn, "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s", (tenant,)
                )
            ),
            "items": int(
                _scalar(conn, "SELECT count(*) FROM catalog_items WHERE tenant_id = %s", (tenant,))
            ),
            "conflicts_open": int(
                _scalar(
                    conn,
                    """SELECT count(*) FROM catalog_conflict_groups
                        WHERE tenant_id = %s AND status = 'OPEN'""",
                    (tenant,),
                )
            ),
        }
    return stats


def _scalar(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
    row = conn.execute(sql, params).fetchone()
    return 0 if row is None else row[0]


_SCOPES = {
    # 内部运营：问答 + 冲突决议 + 文档审核 + 内部发布
    "internal": [
        "knowledge.answer", "knowledge.search", "catalog.read", "catalog.resolve",
        "evidence.read", "knowledge.ingest", "knowledge.read", "knowledge.review",
        "knowledge.publish", "knowledge.revoke", "knowledge.write", "learner",
        "interview.create", "interview.read", "interview.review",
    ],
    # 客户视角：只能问答/检索/看证据，且数据范围只有 customer_public
    "customer": ["knowledge.answer", "knowledge.search", "evidence.read"],
}

_ALLOWED = {
    # 内部运营同时具备内部与对外两类数据范围：他们负责上传/审核对外内容；
    # 反过来客户令牌只有 customer_public，这才是隔离要演示的方向。
    "internal": ["internal_product", "customer_public", "internal_training"],
    "customer": ["customer_public"],
    "publisher": ["internal_product", "customer_public"],
}


def _issue_token(secret: str, tenant: str, principal: str, *, perspective: str = "internal") -> str:
    """签发演示令牌。

    perspective 决定**授权面**，不只是 UI 开关：
    - internal：内部运营（能审、能发布内部、数据范围 internal_product）
    - customer：客户视角（无审核/发布权限，数据范围只有 customer_public）
    - publisher：对外发布审核员（四眼原则里的第二人，可 publish_public）

    用同一套 seed 数据、不同令牌，才能演示"隔离真的在服务端生效"。
    """
    now = datetime.now(UTC)
    scopes = list(_SCOPES.get(perspective, _SCOPES["internal"]))
    roles = ["staff"]
    if perspective == "publisher":
        scopes = ["knowledge.publish_public", "knowledge.review", "knowledge.read"]
        roles = ["public_approver"]
    if perspective == "customer":
        roles = ["customer"]
    payload = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": f"{principal[:8]}@demo.local",
        "iat": now,
        "exp": now + timedelta(hours=12),
        "tenant_id": tenant,
        "principal_id": principal,
        "roles": roles,
        "scope": scopes,
        "allowed_scopes": _ALLOWED[perspective],
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "fkh-demo"
    api_base = "http://127.0.0.1:8000"
    token = ""  # 兼容：未指定视角时代理用内部令牌
    tokens: dict[str, str] = {}
    stats: dict[str, Any] = {}
    provider = "none"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音，避免刷屏
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/conflicts":
            query = f"?{parsed.query}" if parsed.query else ""
            self._proxy("GET", f"/v1/catalog/conflicts{query}", None)
            return
        if path.startswith("/api/interviews/"):
            rest = path.removeprefix("/api/interviews/")
            self._proxy("GET", f"/v1/interviews/{rest}", None)
            return
        if path == "/api/training/assignments":
            self._proxy("GET", "/v1/training/me/assignments", None)
            return
        if path.startswith("/api/training/assignments/"):
            rest = path.removeprefix("/api/training/assignments/")
            assignment_id, _, action = rest.partition("/")
            if action == "content":
                query = f"?{parsed.query}" if parsed.query else ""
                self._proxy(
                    "GET", f"/v1/training/assignments/{assignment_id}/content{query}", None
                )
                return
        if path == "/api/documents":
            query = f"?{parsed.query}" if parsed.query else ""
            self._proxy("GET", f"/v1/knowledge/documents{query}", None)
            return
        if path.startswith("/api/evidence/"):
            evidence_id = path.removeprefix("/api/evidence/")
            self._proxy("GET", f"/v1/evidence/{evidence_id}", None)
            return
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if path in ("/conflicts", "/conflicts/"):
            self._send(200, CONFLICTS_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path in ("/experience", "/experience/"):
            self._send(200, EXPERIENCE_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path in ("/training", "/training/"):
            self._send(200, TRAINING_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path in ("/documents", "/documents/"):
            self._send(200, DOCUMENTS_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/assets/evidence.js":
            self._send(200, EVIDENCE_JS.encode(), "application/javascript; charset=utf-8")
            return
        if path == "/api/status":
            body = {
                "tenant_label": DEMO_TENANT_NAME,
                "chunks": self.stats.get("chunks"),
                "items": self.stats.get("items"),
                "conflicts_open": self.stats.get("conflicts_open"),
                "provider": self.provider,
                # 经验采集页需要"当前演示身份"作为受访者（授权只允许本人）
                "subject": self.stats.get("subject"),
            }
            self._send(200, json.dumps(body, ensure_ascii=False).encode(), "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def _proxy(
        self, method: str, target: str, payload: bytes | None, perspective: str = "internal"
    ) -> None:
        """转发到内部 API。

        perspective 决定用哪套令牌——这是演示"服务端隔离"的关键：
        客户视角走的是另一张令牌（无审核/发布权限、allowed_scopes 只有 customer_public），
        不是前端开关。
        """
        token = self.tokens.get(perspective) or self.token
        request = urllib.request.Request(
            f"{self.api_base}{target}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                self._send(response.status, response.read(), "application/json")
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read(), "application/json")
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"status": "ERROR", "answer": f"演示代理失败：{exc}"}, ensure_ascii=False)
            self._send(502, body.encode(), "application/json")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/interviews"):
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b"{}"
            target = "/v1/interviews" + parsed.path.removeprefix("/api/interviews")
            if parsed.query:
                target += "?" + parsed.query
            self._proxy("POST", target, payload)
            return
        if parsed.path == "/api/training/ask":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b"{}"
            self._proxy("POST", "/v1/training/ask", payload)
            return
        if parsed.path == "/api/documents":
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b"{}"
            self._proxy("POST", "/v1/knowledge/documents", payload)
            return
        if parsed.path.startswith("/api/documents/"):
            rest = parsed.path.removeprefix("/api/documents/")
            version_id, _, action = rest.partition("/")
            if action in ("revoke", "discard"):
                query = f"?{parsed.query}" if parsed.query else ""
                self._proxy(
                    "POST", f"/v1/knowledge/{version_id}/{action}{query}", None
                )
                return
            if action in ("review", "publish"):
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b"{}"
                perspective = "internal"
                if action == "publish":
                    try:
                        if json.loads(payload or b"{}").get("audience") == "customer_public":
                            # 四眼原则：对外发布由第二个主体执行
                            perspective = "publisher"
                    except json.JSONDecodeError:
                        pass
                self._proxy(
                    "POST", f"/v1/knowledge/{version_id}/{action}", payload, perspective
                )
                return
        if parsed.path.startswith("/api/conflicts/") and parsed.path.endswith("/resolve"):
            group_id = parsed.path.removeprefix("/api/conflicts/").removesuffix("/resolve")
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b"{}"
            self._proxy(
                "POST", f"/v1/catalog/conflicts/{group_id}/resolve", payload
            )
            return
        if parsed.path != "/api/answer":
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b"{}"
        perspective = "internal"
        token = self.token
        try:
            body = json.loads(payload or b"{}")
            # perspective 是演示页字段，**任何视角都要从请求体里摘掉**，
            # 否则会被 API 的严格校验拒掉（extra="forbid" → body.perspective: not allowed）
            if body.pop("perspective", "internal") == "customer":
                perspective = "customer"
                token = self.tokens.get("customer") or self.token
                body["knowledge_scope"] = "customer_public"
            payload = json.dumps(body, ensure_ascii=False).encode()
        except json.JSONDecodeError:
            pass
        request = urllib.request.Request(
            f"{self.api_base}/v1/knowledge/answer",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                self._send(response.status, response.read(), "application/json")
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read(), "application/json")
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"status": "ERROR", "answer": f"演示代理失败：{exc}"}, ensure_ascii=False)
            self._send(502, body.encode(), "application/json")


def _wait_health(url: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地私域演示环境")
    parser.add_argument("--pgdata", default=".demo-pgdata")
    parser.add_argument("--db-port", type=int, default=55432)
    parser.add_argument("--corpus", default="_corpus/catalog_2023.md")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=8010)
    parser.add_argument("--provider", default="ollama", choices=["ollama", "none"])
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--secret", default="demo-jwt-secret-0123456789abcdef")
    parser.add_argument("--user", default="demo")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.exists():
        print(f"找不到语料：{corpus}（先把目录文本放到该路径）", file=sys.stderr)
        return 2

    pgdata = Path(args.pgdata).resolve()
    dsn = ensure_postgres(pgdata, args.db_port)
    print(f"[demo] 数据库 {dsn}")
    _apply_migrations(dsn)
    stats = _seed(dsn, corpus, args.user)
    print(f"[demo] 播种完成：{stats}")

    internal_token = _issue_token(args.secret, stats["tenant"], stats["principal"])
    customer_token = _issue_token(
        args.secret, stats["tenant"], stats["principal"], perspective="customer"
    )
    publisher_token = _issue_token(
        args.secret, stats["tenant"], stats.get("public_principal", stats["principal"]),
        perspective="publisher",
    )

    env = {
        **os.environ,
        "FKH_DATABASE_URL": dsn,
        "FKH_JWT_SECRET": args.secret,
        "FKH_RETRIEVAL_HASH_KEY": "demo-retrieval-hmac-key-0123456789",
        "FKH_RETRIEVAL_HASH_KEY_ID": "k-demo",
        "FKH_LLM_PROVIDER": args.provider,
        "FKH_LLM_MODEL": args.model,
    }
    # 环境变量必须在导入 create_app 之前生效（Settings 在模块加载时读取）
    for key, value in env.items():
        os.environ[key] = value

    import uvicorn

    from fkh.api.main import create_app

    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=args.api_port,
        log_level="warning",
    )
    # 非主线程不能装信号处理器（uvicorn 在 serve() 里会尝试），这里显式关掉
    config.install_signal_handlers = False  # type: ignore[attr-defined]
    api_server = uvicorn.Server(config)
    api_thread = threading.Thread(target=api_server.run, name="fkh-api", daemon=True)
    api_thread.start()
    print(f"[demo] API 启动中 127.0.0.1:{args.api_port}")

    DemoHandler.api_base = f"http://127.0.0.1:{args.api_port}"
    DemoHandler.token = internal_token
    DemoHandler.tokens = {
        "internal": internal_token,
        "customer": customer_token,
        "publisher": publisher_token,
    }
    DemoHandler.stats = stats
    DemoHandler.provider = args.provider

    ui = ThreadingHTTPServer(("127.0.0.1", args.ui_port), DemoHandler)
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    print(f"[demo] 演示页 http://127.0.0.1:{args.ui_port}/")

    if not _wait_health(f"http://127.0.0.1:{args.api_port}/health/live"):
        print("[demo] API 未在 60s 内就绪，请检查日志", file=sys.stderr)
        api_server.should_exit = True
        return 1
    print("[demo] 就绪。Ctrl+C 退出（会一并停止 API 与数据库）。")

    try:
        while not api_server.should_exit:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[demo] 收到退出信号")
    finally:
        api_server.should_exit = True
        api_thread.join(timeout=10)
        ui.shutdown()
        stop_postgres(pgdata)
        print("[demo] 已停止 API 与数据库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
