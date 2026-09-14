"""Рушій проходження анкети: стан сесії, наступне питання, крок назад.

Стан (`Session`) серіалізується в JSON, тому однаково працює
і в консолі, і в HTTP API, і в Telegram (зберігається в БД).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .schema import Question, Questionnaire, ValidationError


@dataclass
class Session:
    """Стан однієї анкети конкретного користувача."""

    session_id: str
    channel: str = "cli"                       # cli | api | telegram
    external_user_id: str | None = None        # telegram user_id тощо
    answers: dict[str, Any] = field(default_factory=dict)
    cursor: int = 0                            # індекс у плоскому списку питань
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    order_id: int | None = None

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "channel": self.channel,
            "external_user_id": self.external_user_id,
            "answers": self.answers,
            "cursor": self.cursor,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "completed_at": self.completed_at.isoformat(timespec="seconds")
            if self.completed_at
            else None,
            "order_id": self.order_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            session_id=data["session_id"],
            channel=data.get("channel", "cli"),
            external_user_id=data.get("external_user_id"),
            answers=dict(data.get("answers") or {}),
            cursor=int(data.get("cursor") or 0),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else datetime.now(),
            completed_at=datetime.fromisoformat(data["completed_at"])
            if data.get("completed_at")
            else None,
            order_id=data.get("order_id"),
        )


@dataclass(frozen=True)
class AnswerResult:
    """Що сталося після відповіді користувача."""

    ok: bool
    error: str | None = None
    question: Question | None = None     # наступне питання (або те саме при помилці)
    finished: bool = False
    value: Any = None


@dataclass(frozen=True)
class Progress:
    answered: int
    total: int
    block_number: int
    blocks_total: int
    block_title: str

    @property
    def percent(self) -> int:
        return int(round(100 * self.answered / self.total)) if self.total else 100

    def bar(self, width: int = 10) -> str:
        filled = int(round(width * self.answered / self.total)) if self.total else width
        return "▓" * filled + "░" * (width - filled)


class QuestionnaireEngine:
    """Веде користувача по питаннях, пропускаючи неактивні."""

    def __init__(self, questionnaire: Questionnaire):
        self.q = questionnaire
        self._flat: list[Question] = list(questionnaire.questions)

    # -- життєвий цикл --------------------------------------------------
    def start(
        self,
        channel: str = "cli",
        external_user_id: str | None = None,
        prefill: dict[str, Any] | None = None,
    ) -> Session:
        session = Session(
            session_id=uuid.uuid4().hex[:12],
            channel=channel,
            external_user_id=external_user_id,
            answers=dict(prefill or {}),
        )
        session.cursor = self._advance(session, 0)
        return session

    def current(self, session: Session) -> Question | None:
        """Питання, на яке зараз чекаємо. None = анкету пройдено."""
        idx = self._advance(session, session.cursor)
        session.cursor = idx
        return self._flat[idx] if idx < len(self._flat) else None

    def submit(self, session: Session, raw: Any) -> AnswerResult:
        """Прийняти відповідь на поточне питання."""
        question = self.current(session)
        if question is None:
            return AnswerResult(ok=True, finished=True)

        try:
            value = question.parse(raw)
        except ValidationError as exc:
            return AnswerResult(ok=False, error=str(exc), question=question)

        session.answers[question.id] = value
        self._drop_orphans(session)
        session.cursor = self._advance(session, session.cursor + 1)

        nxt = self._flat[session.cursor] if session.cursor < len(self._flat) else None
        if nxt is None:
            session.completed_at = datetime.now()
        return AnswerResult(ok=True, question=nxt, finished=nxt is None, value=value)

    def back(self, session: Session) -> Question | None:
        """Крок назад: прибирає попередню відповідь."""
        idx = min(session.cursor, len(self._flat)) - 1
        while idx >= 0 and not self._flat[idx].is_active(session.answers):
            idx -= 1
        if idx < 0:
            return self.current(session)
        session.answers.pop(self._flat[idx].id, None)
        session.completed_at = None
        session.cursor = idx
        return self._flat[idx]

    def restart(self, session: Session) -> Question | None:
        session.answers.clear()
        session.completed_at = None
        session.order_id = None
        session.cursor = self._advance(session, 0)
        return self.current(session)

    # -- довідкове ------------------------------------------------------
    def progress(self, session: Session) -> Progress:
        active = [q for q in self._flat if q.is_active(session.answers)]
        answered = sum(1 for q in active if q.id in session.answers)
        current = self.current(session)
        if current is not None:
            block_no = self.q.block_index(current) + 1
            block_title = self.q.block_of(current).title
        else:
            block_no, block_title = len(self.q.blocks), "Готово"
        return Progress(
            answered=answered,
            total=len(active),
            block_number=block_no,
            blocks_total=len(self.q.blocks),
            block_title=block_title,
        )

    def missing(self, session: Session) -> list[Question]:
        """Обов'язкові питання без відповіді (страховка перед розрахунком)."""
        return [
            q
            for q in self._flat
            if q.is_active(session.answers)
            and q.required
            and session.answers.get(q.id) is None
        ]

    def answers_summary(self, session: Session) -> list[tuple[str, str]]:
        """Пари (питання, відповідь) для підтвердження перед відправкою."""
        out: list[tuple[str, str]] = []
        for q in self._flat:
            if not q.is_active(session.answers) or q.id not in session.answers:
                continue
            value = session.answers[q.id]
            if isinstance(value, bool):
                shown = "так" if value else "ні"
            elif q.type == "choice":
                shown = q.option_label(value)
            elif q.type == "multichoice":
                shown = ", ".join(q.option_label(v) for v in value or [])
            elif value is None:
                shown = "—"
            else:
                shown = f"{value}{(' ' + q.unit) if q.unit else ''}"
            out.append((q.text, shown))
        return out

    # -- внутрішнє ------------------------------------------------------
    def _advance(self, session: Session, idx: int) -> int:
        """Перший активний індекс, починаючи з idx."""
        while idx < len(self._flat) and not self._flat[idx].is_active(session.answers):
            session.answers.pop(self._flat[idx].id, None)
            idx += 1
        return idx

    def _drop_orphans(self, session: Session) -> None:
        """Прибрати відповіді, які стали неактуальними після зміни умови."""
        for q in self._flat:
            if not q.is_active(session.answers):
                session.answers.pop(q.id, None)


# --------------------------------------------------------------------------
def render_question(question: Question, progress: Progress | None = None) -> str:
    """Текст питання для чату/консолі (у Telegram варіанти йдуть кнопками)."""
    lines: list[str] = []
    if progress:
        lines.append(
            f"[{progress.bar()}] {progress.percent}%  "
            f"блок {progress.block_number}/{progress.blocks_total}"
        )
    lines.append(question.text + ("" if question.required else "  (необов'язково)"))
    if question.options:
        lines += [f"  {i}. {o.label}" for i, o in enumerate(question.options, 1)]
    if question.type == "bool":
        lines.append("  так / ні")
    if question.help:
        lines.append(f"ℹ {question.help}")
    if question.placeholder:
        lines.append(f"Напр.: {question.placeholder}")
    if not question.required:
        lines.append("Щоб пропустити — надішліть «-».")
    return "\n".join(lines)
