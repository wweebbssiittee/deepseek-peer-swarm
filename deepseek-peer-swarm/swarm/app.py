from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .a2a import install_a2a
from .config import Settings, data_directory
from .engine import Engine
from .notifications import Notifier
from .store import Store


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Permissions(Input):
    read_files: bool = True
    write_files: bool = False
    internet: bool = True
    commands: Literal["deny", "ask", "allow"] = "ask"
    deploy: Literal["deny", "ask", "allow"] = "ask"


class NewRun(Input):
    task: str = Field(min_length=1, max_length=30000)
    workspace: str = Field(default="", max_length=2048)
    permissions: Permissions = Field(default_factory=Permissions)
    max_rounds: int = Field(default=500, ge=1, le=100000)
    max_tokens: int = Field(default=1000000000, ge=10000, le=1000000000)
    max_minutes: int = Field(default=1440, ge=1, le=43200)
    budget_usd: Decimal = Field(default=Decimal("1.00"), ge=Decimal("0.01"), le=Decimal("100000"), max_digits=8, decimal_places=2)
    stall_minutes: int = Field(default=10, ge=1, le=1440)


class SettingsInput(Input):
    model: str | None = Field(default=None, min_length=1, max_length=120)
    thinking: bool | None = None
    reasoning_effort: Literal["low", "high", "max"] | None = None
    max_output_tokens: int | None = Field(default=None, ge=512, le=65536)
    system_prompt: str | None = Field(default=None, min_length=100, max_length=30000)
    keys: list[str] | None = Field(default=None, min_length=10, max_length=10)


class Chat(Input):
    text: str = Field(min_length=1, max_length=30000)
    to: str = "all"


class Control(Input):
    action: Literal["pause", "resume", "stop"]


class Approval(Input):
    approved: bool


class NotificationSettings(Input):
    enabled: StrictBool


class Limits(Input):
    max_rounds: int = Field(ge=1, le=100000)
    max_tokens: int = Field(ge=10000, le=1000000000)
    max_minutes: int = Field(ge=1, le=43200)
    max_output_tokens: int | None = Field(default=None, ge=512, le=65536)
    budget_usd: Decimal | None = Field(default=None, ge=Decimal("0.01"), le=Decimal("100000"), max_digits=8, decimal_places=2)
    stall_minutes: int | None = Field(default=None, ge=1, le=1440)


def create_app(home: Path | None = None, provider=None, port=8767, notifier=None):
    settings = Settings(home or data_directory())
    store = Store(settings.home / "swarm.sqlite")
    notifications = notifier if notifier is not None else Notifier(settings.home)
    engine = Engine(store, settings, provider, notifier=notifications)
    engine.public_url = f"http://127.0.0.1:{port}"

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        try:
            yield
        finally:
            try:
                await engine.close()
            finally:
                try:
                    await notifications.close()
                finally:
                    store.close()

    app = FastAPI(title="DeepSeek Peer Swarm", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.settings = settings
    app.state.notifier = notifications
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if origin and origin not in allowed or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        if content_length < 0 or content_length > 1500000:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            authorization = request.headers.get("authorization", "")
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else request.headers.get("x-swarm-token", "")
            if not secrets.compare_digest(token, settings.token):
                return JSONResponse({"detail": "Local access token required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": settings.redact(str(error))}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # FastAPI's default handler echoes the rejected input, including API keys
        # that have not been saved yet and cannot be recognized by redact().
        return JSONResponse(settings.redact_value({"detail": [
            {"type": item["type"], "loc": item["loc"], "msg": item["msg"]}
            for item in error.errors()
        ]}), status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": "Task or item not found"}, status_code=404)

    @app.get("/api/health")
    async def health():
        return {"app": "deepseek-peer-swarm", "status": "ok", "peers": 10}

    @app.get("/api/bootstrap")
    async def bootstrap():
        return {"csrf_token": settings.token, "settings": settings.public(), "notifications": notifications.public(), "runs": engine.summaries(), "default_workspace": str(settings.home / "workspaces")}

    @app.post("/api/notifications")
    async def configure_notifications(body: NotificationSettings):
        notifications.configure(body.enabled)
        return notifications.public()

    @app.post("/api/notifications/test")
    async def test_notification():
        await notifications.test()
        return notifications.public()

    @app.post("/api/settings")
    async def configure(body: SettingsInput):
        if any(s["run"]["status"] in ("running", "waiting", "pausing") for s in engine.states.values()):
            raise ValueError("Pause the active task before changing model settings or keys")
        settings.update(body.model_dump(exclude_none=True))
        return settings.public()

    @app.post("/api/runs")
    async def create(body: NewRun):
        if not body.task.strip():
            raise ValueError("Enter a task")
        return settings.redact_value(await engine.create(body.model_dump(mode="json")))

    @app.get("/api/runs/{run_id}")
    async def snapshot(run_id: str):
        return engine.snapshot(run_id)

    @app.post("/api/runs/{run_id}/messages")
    async def chat(run_id: str, body: Chat):
        state = engine.state(run_id)
        if state["run"]["status"] in ("completed", "stopped"):
            raise ValueError("Create a new task to continue after a completed or stopped run")
        if not body.text.strip():
            raise ValueError("Enter a message")
        return settings.redact_value(engine.message(state, "user", body.to, body.text))

    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, body: Control):
        return settings.redact_value(await engine.control(run_id, body.action))

    @app.post("/api/runs/{run_id}/permissions")
    async def permissions(run_id: str, body: Permissions):
        return settings.redact_value(await engine.set_permissions(run_id, body.model_dump()))

    @app.post("/api/runs/{run_id}/limits")
    async def limits(run_id: str, body: Limits):
        state = engine.state(run_id)
        if state["run"]["status"] == "pausing":
            raise ValueError("Wait for workers to finish stopping")
        values = body.model_dump(exclude_none=True, mode="json")
        if "budget_usd" in values:
            from .billing import parse_budget
            bill = engine.ensure_billing(state)
            budget = parse_budget(values["budget_usd"])
            committed = sum(bill[k] for k in ("spent_nusd", "reserved_nusd", "uncertain_nusd"))
            if budget < committed:
                raise ValueError("The budget cannot be lower than costs already spent or held for active/uncertain requests")
            bill["budget_nusd"] = budget
            values["budget_usd"] = budget / 1000000000
        state["run"].update(values)
        if "max_output_tokens" in values:
            state["config"]["max_output_tokens"] = values["max_output_tokens"]
        engine.save(state)
        engine.event(state, "limits_changed", data=values)
        return settings.redact_value(state["run"])

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def approve(run_id: str, approval_id: str, body: Approval):
        return settings.redact_value(engine.resolve_approval(run_id, approval_id, body.approved))

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: str):
        return JSONResponse(settings.redact_value({**engine.snapshot(run_id), "api_calls": store.calls(run_id, limit=None)}), headers={"Content-Disposition": f'attachment; filename="swarm-{run_id}.json"'})

    @app.get("/api/runs/{run_id}/costs")
    async def costs(run_id: str):
        state = engine.state(run_id)
        return settings.redact_value({"billing": engine.billing_summary(state), "calls": store.calls(run_id)})

    @app.post("/api/shutdown")
    async def shutdown():
        for run_id in list(engine.workers):
            if engine.state(run_id)["run"]["status"] in ("running", "waiting"):
                await engine.control(run_id, "pause")
        if hasattr(app.state, "request_shutdown"):
            app.state.request_shutdown()
        return {"ok": True}

    install_a2a(app, engine)
    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app
