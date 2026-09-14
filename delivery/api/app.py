"""HTTP API поверх того самого ядра — для сайту, CRM або зовнішнього бота.

Запуск:
    pip install fastapi "uvicorn[standard]"
    python -m delivery.api.app          # або: uvicorn delivery.api.app:app

Ендпоінти:
    POST /api/sessions                  почати анкету
    GET  /api/sessions/{id}             поточне питання і прогрес
    POST /api/sessions/{id}/answer      відповісти
    POST /api/sessions/{id}/back        крок назад
    POST /api/sessions/{id}/quote       порахувати вартість
    POST /api/sessions/{id}/manager     покликати менеджера
    POST /api/quote                     разовий розрахунок без анкети
    GET  /api/distance?from=&to=        лише кілометраж
    GET  /api/orders                    останні заявки (потрібен токен)
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..geo.base import GeoError
from ..models import PricingInput
from ..pricing import calculate
from ..services.manager import ManagerService
from ..services.order_service import OrderService

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        'Не встановлено FastAPI. Виконайте: pip install fastapi "uvicorn[standard]"'
    ) from exc

settings = Settings.load()
service = OrderService(settings)
manager_service = ManagerService(service.repo, settings)

app = FastAPI(title="Розрахунок доставки", version="1.0.0")


# --------------------------------------------------------------------------
class StartRequest(BaseModel):
    channel: str = "api"
    user_id: str | None = None
    resume: bool = True


class AnswerRequest(BaseModel):
    value: Any


class ManagerRequest(BaseModel):
    reason: str = "запит через API"


class QuoteRequest(BaseModel):
    """Разовий розрахунок, коли анкету веде зовнішня система."""

    pickup: str
    dropoff: str
    weight_kg: float = Field(gt=0)
    volume_m3: float = 0
    vehicle: str = "gazelle"
    cargo_type: str = "general"
    urgency: str = "standard"
    temperature_mode: str = "none"
    loaders: int = 0
    tail_lift: bool = False
    extra_stops: int = 0
    return_trip: bool = False
    declared_value: float = 0
    insurance: bool = False
    vat: bool = False


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """Захист службових ендпоінтів. Якщо API_TOKEN порожній — перевірки немає."""
    if settings.api_token and x_api_token != settings.api_token:
        raise HTTPException(status_code=401, detail="Невірний або відсутній X-API-Token")


def _question_payload(session) -> dict[str, Any]:
    question = service.engine.current(session)
    progress = service.progress(session)
    payload: dict[str, Any] = {
        "session_id": session.session_id,
        "finished": question is None,
        "progress": {
            "answered": progress.answered,
            "total": progress.total,
            "percent": progress.percent,
            "block_number": progress.block_number,
            "blocks_total": progress.blocks_total,
            "block_title": progress.block_title,
        },
    }
    if question is not None:
        payload["question"] = {
            "id": question.id,
            "text": question.text,
            "type": question.type,
            "required": question.required,
            "help": question.help,
            "unit": question.unit,
            "options": [{"value": o.value, "label": o.label} for o in question.options],
        }
    return payload


def _load(session_id: str):
    session = service.repo.load_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Сесію не знайдено")
    return session


# --------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "tariff_version": service.tariff.version,
        "geo_provider": settings.geo_provider,
        "questions": len(service.questionnaire.questions),
    }


@app.post("/api/sessions")
def start_session(request: StartRequest) -> dict[str, Any]:
    session, _ = service.start(
        channel=request.channel, user_id=request.user_id, resume=request.resume
    )
    return _question_payload(session)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return _question_payload(_load(session_id))


@app.post("/api/sessions/{session_id}/answer")
def answer(session_id: str, request: AnswerRequest) -> dict[str, Any]:
    session = _load(session_id)
    result = service.answer(session, request.value)
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return _question_payload(session)


@app.post("/api/sessions/{session_id}/back")
def back(session_id: str) -> dict[str, Any]:
    session = _load(session_id)
    service.back(session)
    return _question_payload(session)


@app.post("/api/sessions/{session_id}/quote")
def quote(session_id: str) -> dict[str, Any]:
    session = _load(session_id)
    result = service.quote(session)
    if not result.ok:
        raise HTTPException(
            status_code=409,
            detail={
                "error": result.error,
                "needs_manager": result.needs_manager,
                "order_id": result.order.id if result.order else None,
            },
        )
    return {"order_id": result.order.id, "quote": result.quote.as_dict()}


@app.post("/api/sessions/{session_id}/manager")
def call_manager(session_id: str, request: ManagerRequest) -> dict[str, Any]:
    session = _load(session_id)
    result = manager_service.request(
        session_id=session.session_id,
        channel=session.channel,
        client_ref=session.external_user_id or session.session_id,
        client_name=str(session.answers.get("client_name") or ""),
        client_phone=str(session.answers.get("client_phone") or ""),
        reason=request.reason,
    )
    return {"handoff_id": result.handoff_id, "message": result.message}


@app.post("/api/quote")
def quote_once(request: QuoteRequest) -> dict[str, Any]:
    try:
        route = service.geo.route([request.pickup, request.dropoff])
    except GeoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    data = PricingInput(
        distance_km=route.distance_km,
        **request.model_dump(exclude={"pickup", "dropoff"}),
    )
    return calculate(data, service.tariff, route).as_dict()


@app.get("/api/distance")
def distance(
    from_: str = Query(..., alias="from", description="Звідки"),
    to: str = Query(..., description="Куди"),
) -> dict[str, Any]:
    try:
        route = service.geo.route([from_, to])
    except GeoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return route.as_dict()


@app.get("/api/orders", dependencies=[Depends(require_token)])
def orders(limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
    return service.repo.recent_orders(limit=limit, status=status)


@app.get("/api/stats", dependencies=[Depends(require_token)])
def stats() -> dict[str, Any]:
    return service.repo.stats()


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":  # pragma: no cover
    main()
