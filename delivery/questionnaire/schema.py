"""Опис анкети та валідація відповідей.

Анкета описується в YAML/JSON (config/questions.yaml) і завантажується
сюди. Кожне питання саме вміє розібрати сиру відповідь користувача
(`Question.parse`) — тому логіка однакова в CLI, API і в Telegram.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SKIP_WORDS = {"-", "—", "пропустити", "пропуск", "skip", "немає", "нема", "no", "нет"}
TRUE_WORDS = {"так", "да", "yes", "y", "+", "1", "true", "ага", "ок", "okay", "ok"}
FALSE_WORDS = {"ні", "нi", "нет", "no", "n", "-", "0", "false"}


class ValidationError(ValueError):
    """Відповідь користувача не пройшла перевірку. Текст — уже для клієнта."""


class SchemaError(Exception):
    """Помилка в самому файлі анкети."""


# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Option:
    value: str
    label: str

    @classmethod
    def from_raw(cls, raw: Any) -> "Option":
        if isinstance(raw, dict):
            value = str(raw.get("value", "")).strip()
            if not value:
                raise SchemaError(f"варіант без 'value': {raw!r}")
            return cls(value=value, label=str(raw.get("label") or value))
        return cls(value=str(raw), label=str(raw))


@dataclass(frozen=True)
class Condition:
    """Умова показу питання: залежність від попередньої відповіді."""

    question: str
    equals: Any = None
    one_of: tuple[Any, ...] = ()

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "Condition | None":
        if not raw:
            return None
        qid = raw.get("question") or raw.get("id")
        if not qid:
            raise SchemaError(f"depends_on без 'question': {raw!r}")
        one_of = raw.get("one_of") or raw.get("in") or ()
        return cls(question=str(qid), equals=raw.get("equals"), one_of=tuple(one_of))

    def matches(self, answers: dict[str, Any]) -> bool:
        value = answers.get(self.question)
        if self.one_of:
            return value in self.one_of
        if self.equals is not None:
            return value == self.equals
        return value not in (None, "", [], False)


# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Question:
    id: str
    block_id: str
    text: str
    type: str = "text"
    required: bool = True
    options: tuple[Option, ...] = ()
    help: str = ""
    placeholder: str = ""
    unit: str = ""
    min: float | None = None
    max: float | None = None
    min_len: int | None = None
    max_len: int | None = None
    default: Any = None
    depends_on: Condition | None = None

    # -- побудова ------------------------------------------------------
    @classmethod
    def from_raw(cls, raw: dict[str, Any], block_id: str) -> "Question":
        qid = str(raw.get("id") or "").strip()
        if not qid:
            raise SchemaError(f"питання без 'id' у блоці {block_id}")
        qtype = str(raw.get("type") or "text").strip().lower()
        if qtype not in PARSERS:
            raise SchemaError(
                f"питання '{qid}': невідомий тип '{qtype}'. "
                f"Доступні: {', '.join(sorted(PARSERS))}"
            )
        options = tuple(Option.from_raw(o) for o in raw.get("options") or ())
        if qtype in {"choice", "multichoice"} and not options:
            raise SchemaError(f"питання '{qid}' типу {qtype} потребує 'options'")
        return cls(
            id=qid,
            block_id=block_id,
            text=str(raw.get("text") or qid),
            type=qtype,
            required=bool(raw.get("required", True)),
            options=options,
            help=str(raw.get("help") or ""),
            placeholder=str(raw.get("placeholder") or ""),
            unit=str(raw.get("unit") or ""),
            min=_num_or_none(raw.get("min")),
            max=_num_or_none(raw.get("max")),
            min_len=_int_or_none(raw.get("min_len")),
            max_len=_int_or_none(raw.get("max_len")),
            default=raw.get("default"),
            depends_on=Condition.from_raw(raw.get("depends_on")),
        )

    # -- поведінка -----------------------------------------------------
    def is_active(self, answers: dict[str, Any]) -> bool:
        """Чи треба ставити це питання при таких відповідях."""
        return self.depends_on is None or self.depends_on.matches(answers)

    def option_label(self, value: Any) -> str:
        for opt in self.options:
            if opt.value == value:
                return opt.label
        return str(value)

    def parse(self, raw: Any) -> Any:
        """Сира відповідь → типізоване значення. Кидає ValidationError."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if self.default is not None:
                return self.default
            if self.required:
                raise ValidationError("Будь ласка, дайте відповідь на це питання.")
            return None

        if isinstance(raw, str) and raw.strip().lower() in SKIP_WORDS:
            if self.type == "bool":
                pass  # для bool «-» означає «ні», хай розбирає парсер
            elif not self.required:
                return self.default
            elif self.default is not None:
                return self.default
            else:
                raise ValidationError("Це питання обов'язкове — його не можна пропустити.")

        return PARSERS[self.type](self, raw)


@dataclass(frozen=True)
class Block:
    id: str
    title: str
    description: str = ""
    questions: tuple[Question, ...] = ()

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Block":
        bid = str(raw.get("id") or "").strip()
        if not bid:
            raise SchemaError(f"блок без 'id': {raw.get('title')!r}")
        questions = tuple(Question.from_raw(q, bid) for q in raw.get("questions") or ())
        if not questions:
            raise SchemaError(f"блок '{bid}' не містить питань")
        return cls(
            id=bid,
            title=str(raw.get("title") or bid),
            description=str(raw.get("description") or ""),
            questions=questions,
        )


@dataclass(frozen=True)
class Questionnaire:
    version: int
    title: str
    blocks: tuple[Block, ...]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Questionnaire":
        blocks = tuple(Block.from_raw(b) for b in raw.get("blocks") or ())
        if not blocks:
            raise SchemaError("анкета не містить жодного блоку")
        seen: set[str] = set()
        for q in (q for b in blocks for q in b.questions):
            if q.id in seen:
                raise SchemaError(f"дубльований id питання: '{q.id}'")
            seen.add(q.id)
        for q in (q for b in blocks for q in b.questions):
            if q.depends_on and q.depends_on.question not in seen:
                raise SchemaError(
                    f"питання '{q.id}' залежить від невідомого '{q.depends_on.question}'"
                )
        return cls(
            version=int(raw.get("version") or 1),
            title=str(raw.get("title") or "Анкета"),
            blocks=blocks,
        )

    @property
    def questions(self) -> tuple[Question, ...]:
        return tuple(q for b in self.blocks for q in b.questions)

    def get(self, question_id: str) -> Question:
        for q in self.questions:
            if q.id == question_id:
                return q
        raise KeyError(question_id)

    def block_of(self, question: Question) -> Block:
        for b in self.blocks:
            if b.id == question.block_id:
                return b
        raise KeyError(question.block_id)

    def block_index(self, question: Question) -> int:
        return [b.id for b in self.blocks].index(question.block_id)


def load_questionnaire(path: str | Path) -> Questionnaire:
    """Читає анкету з .yaml або .json."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise SchemaError(
                "Для .yaml потрібен PyYAML (pip install PyYAML) "
                "або збережіть анкету у .json"
            ) from exc
        raw = yaml.safe_load(text)
    return Questionnaire.from_raw(raw)


# --------------------------------------------------------------------------
#  Парсери типів
# --------------------------------------------------------------------------
def _num_or_none(v: Any) -> float | None:
    return None if v is None else float(v)


def _int_or_none(v: Any) -> int | None:
    return None if v is None else int(v)


def _parse_text(q: Question, raw: Any) -> str:
    value = str(raw).strip()
    if q.min_len and len(value) < q.min_len:
        raise ValidationError(f"Замало символів — потрібно щонайменше {q.min_len}.")
    if q.max_len and len(value) > q.max_len:
        raise ValidationError(f"Задовго — максимум {q.max_len} символів.")
    return value


def _parse_address(q: Question, raw: Any) -> str:
    value = str(raw).strip()
    if len(value) < 2:
        raise ValidationError("Вкажіть, будь ласка, населений пункт або адресу.")
    return re.sub(r"\s{2,}", " ", value)


def _parse_phone(q: Question, raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+38" + digits
    if len(digits) == 9:
        return "+380" + digits
    if 10 <= len(digits) <= 15:      # іноземний номер
        return "+" + digits
    raise ValidationError("Схоже на некоректний номер. Приклад: +380671234567")


def _parse_email(q: Question, raw: Any) -> str:
    value = str(raw).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", value):
        raise ValidationError("Некоректний email. Приклад: name@example.com")
    return value


def _parse_number(q: Question, raw: Any) -> float:
    text = str(raw).strip().replace(",", ".").replace(" ", "")
    text = re.sub(r"[^\d.\-]", "", text)
    try:
        value = float(text)
    except ValueError:
        raise ValidationError("Потрібне число. Приклад: 1200 або 1.5") from None
    if q.min is not None and value < q.min:
        raise ValidationError(f"Значення не може бути меншим за {q.min:g}{_unit(q)}.")
    if q.max is not None and value > q.max:
        raise ValidationError(
            f"Значення завелике (максимум {q.max:g}{_unit(q)}). "
            "Для таких обсягів потрібен окремий прорахунок менеджера."
        )
    return value


def _parse_int(q: Question, raw: Any) -> int:
    value = _parse_number(q, raw)
    if abs(value - round(value)) > 1e-9:
        raise ValidationError("Потрібне ціле число.")
    return int(round(value))


def _parse_bool(q: Question, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    raise ValidationError("Відповідайте «так» або «ні».")


def _parse_choice(q: Question, raw: Any) -> str:
    value = str(raw).strip()
    lowered = value.lower()
    for opt in q.options:
        if lowered in (opt.value.lower(), opt.label.lower()):
            return opt.value
    if value.isdigit():
        idx = int(value) - 1
        if 0 <= idx < len(q.options):
            return q.options[idx].value
    # часткове співпадіння по тексту кнопки
    matches = [o for o in q.options if lowered and lowered in o.label.lower()]
    if len(matches) == 1:
        return matches[0].value
    raise ValidationError(
        "Оберіть один з варіантів:\n"
        + "\n".join(f"{i}. {o.label}" for i, o in enumerate(q.options, 1))
    )


def _parse_multichoice(q: Question, raw: Any) -> list[str]:
    parts: Iterable[Any]
    parts = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    values: list[str] = []
    for part in parts:
        value = _parse_choice(q, part)
        if value not in values:
            values.append(value)
    if not values and q.required:
        raise ValidationError("Оберіть хоча б один варіант.")
    return values


def _parse_date(q: Question, raw: Any) -> str:
    if isinstance(raw, date):
        return raw.isoformat()
    value = str(raw).strip().lower()
    today = date.today()
    shortcuts = {
        "сьогодні": 0, "сегодня": 0, "today": 0,
        "завтра": 1, "tomorrow": 1,
        "післязавтра": 2, "післязавтра.": 2, "послезавтра": 2,
    }
    if value in shortcuts:
        return (today + timedelta(days=shortcuts[value])).isoformat()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m"):
        try:
            parsed = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m":
            parsed = parsed.replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
        if parsed < today:
            raise ValidationError("Дата вже минула. Вкажіть сьогоднішню або майбутню.")
        if (parsed - today).days > 365:
            raise ValidationError("Дата задалеко в майбутньому (більше року).")
        return parsed.isoformat()
    raise ValidationError("Не розпізнав дату. Приклад: 25.12.2026, або «завтра».")


def _unit(q: Question) -> str:
    return f" {q.unit}" if q.unit else ""


PARSERS = {
    "text": _parse_text,
    "address": _parse_address,
    "phone": _parse_phone,
    "email": _parse_email,
    "number": _parse_number,
    "int": _parse_int,
    "bool": _parse_bool,
    "choice": _parse_choice,
    "multichoice": _parse_multichoice,
    "date": _parse_date,
}
