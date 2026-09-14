"""Оркестрація: анкета → кілометраж → ціна → збережена заявка.

Це головна точка входу для будь-якого адаптера (CLI, HTTP API, Telegram).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..geo.base import GeoError, build_provider
from ..models import Order, Quote, RouteInfo
from ..pricing import calculate, load_tariff
from ..pricing.tariff import Tariff, TariffError
from ..questionnaire import (
    AnswerResult,
    Question,
    QuestionnaireEngine,
    Session,
    load_questionnaire,
)
from ..storage import Repository
from .mapping import answers_to_pricing_input, route_points


@dataclass(frozen=True)
class QuoteResult:
    """Результат розрахунку: або ціна, або зрозуміла клієнту помилка."""

    ok: bool
    quote: Quote | None = None
    order: Order | None = None
    error: str | None = None
    needs_manager: bool = False       # True → варто одразу підключити менеджера


class OrderService:
    def __init__(self, settings: Settings | None = None, repo: Repository | None = None):
        self.settings = settings or Settings.load()
        self.questionnaire = load_questionnaire(self.settings.path(self.settings.questions_path))
        self.engine = QuestionnaireEngine(self.questionnaire)
        self.repo = repo or Repository(self.settings.path(self.settings.db_path))
        self.geo = build_provider(self.settings)
        self._tariff: Tariff | None = None

    # ---------------- тарифи ----------------
    @property
    def tariff(self) -> Tariff:
        if self._tariff is None:
            self._tariff = load_tariff(self.settings.path(self.settings.tariffs_path))
        return self._tariff

    def reload_tariff(self) -> Tariff:
        """Підхопити змінені тарифи без перезапуску процесу."""
        self._tariff = load_tariff(self.settings.path(self.settings.tariffs_path))
        return self._tariff

    # ---------------- анкета ----------------
    def start(self, channel: str = "cli", user_id: str | None = None,
              resume: bool = True) -> tuple[Session, Question | None]:
        if resume and user_id:
            existing = self.repo.find_active_session(channel, str(user_id))
            if existing:
                return existing, self.engine.current(existing)
        session = self.engine.start(channel=channel, external_user_id=user_id)
        self.repo.save_session(session)
        return session, self.engine.current(session)

    def answer(self, session: Session, raw: Any) -> AnswerResult:
        result = self.engine.submit(session, raw)
        self.repo.save_session(session)
        return result

    def back(self, session: Session) -> Question | None:
        question = self.engine.back(session)
        self.repo.save_session(session)
        return question

    def restart(self, session: Session) -> Question | None:
        question = self.engine.restart(session)
        self.repo.save_session(session)
        return question

    # ---------------- розрахунок ----------------
    def measure_route(self, answers: dict[str, Any]) -> RouteInfo:
        points = route_points(answers)
        if len(points) < 2:
            raise GeoError("Не вказано міста завантаження або розвантаження.")
        return self.geo.route(points)

    def quote(self, session: Session) -> QuoteResult:
        """Порахувати вартість і зберегти заявку."""
        missing = self.engine.missing(session)
        if missing:
            return QuoteResult(
                ok=False,
                error="Не заповнені обов'язкові питання: "
                + ", ".join(q.text for q in missing),
            )

        try:
            route = self.measure_route(session.answers)
        except GeoError as exc:
            order = self._save_order(session, None, status="manager_requested")
            return QuoteResult(
                ok=False,
                order=order,
                error=f"{exc} Заявку збережено — менеджер порахує вручну.",
                needs_manager=True,
            )

        try:
            pricing_input = answers_to_pricing_input(session.answers, route.distance_km)
            quote = calculate(pricing_input, self.tariff, route)
        except TariffError as exc:
            order = self._save_order(session, None, status="manager_requested")
            return QuoteResult(
                ok=False,
                order=order,
                error=f"Помилка в тарифах: {exc}",
                needs_manager=True,
            )

        order = self._save_order(session, quote)
        return QuoteResult(ok=True, quote=quote, order=order)

    def _save_order(self, session: Session, quote: Quote | None,
                    status: str = "new") -> Order:
        order = Order(
            id=session.order_id,
            session_id=session.session_id,
            channel=session.channel,
            external_user_id=session.external_user_id,
            answers=dict(session.answers),
            quote=quote,
            status=status,
        )
        order.id = self.repo.save_order(order)
        session.order_id = order.id
        self.repo.save_session(session)
        return order

    # ---------------- допоміжне ----------------
    def summary_lines(self, session: Session) -> list[tuple[str, str]]:
        return self.engine.answers_summary(session)

    def progress(self, session: Session):
        return self.engine.progress(session)
