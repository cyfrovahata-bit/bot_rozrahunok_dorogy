"""Telegram-бот (aiogram 3) поверх ядра розрахунку.

Запуск:
    pip install aiogram
    # у .env: TELEGRAM_BOT_TOKEN=..., MANAGER_CHAT_IDS=...
    python -m delivery.bot.telegram

Бот — лише інтерфейс: усі рішення ухвалює OrderService/ManagerService,
тому логіка однакова в боті, в API і в консолі.

Команди клієнта:  /start /restart /manager /help
Команди менеджера: /queue /take <id> /close /orders /stats /reload
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import Settings
from ..questionnaire.schema import Question
from ..services.manager import ManagerService
from ..services.order_service import OrderService
from ..storage import Repository

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.enums import ParseMode
    from aiogram.filters import Command, CommandStart
    from aiogram.types import (
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Не встановлено aiogram. Виконайте: pip install aiogram>=3.4"
    ) from exc

log = logging.getLogger("delivery.bot")

CB_ANSWER = "a"       # a:<індекс варіанта>
CB_SKIP = "skip"
CB_BACK = "back"
CB_MANAGER = "manager"
CB_RESTART = "restart"
CB_CLAIM = "claim"    # claim:<handoff_id>


# --------------------------------------------------------------------------
#  Клавіатури
# --------------------------------------------------------------------------
def question_keyboard(question: Question) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if question.type == "choice":
        rows += [
            [InlineKeyboardButton(text=option.label, callback_data=f"{CB_ANSWER}:{i}")]
            for i, option in enumerate(question.options)
        ]
    elif question.type == "bool":
        rows.append(
            [
                InlineKeyboardButton(text="✅ Так", callback_data=f"{CB_ANSWER}:yes"),
                InlineKeyboardButton(text="❌ Ні", callback_data=f"{CB_ANSWER}:no"),
            ]
        )

    service_row = [InlineKeyboardButton(text="◀ Назад", callback_data=CB_BACK)]
    if not question.required:
        service_row.append(InlineKeyboardButton(text="Пропустити ▶", callback_data=CB_SKIP))
    rows.append(service_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quote_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Підключити менеджера", callback_data=CB_MANAGER)],
            [InlineKeyboardButton(text="🔄 Новий розрахунок", callback_data=CB_RESTART)],
        ]
    )


def claim_keyboard(handoff_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Взяти в роботу", callback_data=f"{CB_CLAIM}:{handoff_id}")]
        ]
    )


# --------------------------------------------------------------------------
#  Доставка повідомлень для ManagerService
# --------------------------------------------------------------------------
class TelegramNotifier:
    """Синхронний інтерфейс ManagerService → асинхронні виклики aiogram."""

    def __init__(self, bot: "Bot", manager_chat_ids: tuple[int, ...]):
        self.bot = bot
        self.manager_chat_ids = manager_chat_ids
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, chat_id: Any, text: str, markup=None) -> None:
        try:
            await self.bot.send_message(int(chat_id), text, reply_markup=markup)
        except Exception as exc:  # заблокований бот, видалений чат тощо
            log.warning("Не вдалося надіслати повідомлення %s: %s", chat_id, exc)

    def notify_managers(self, text: str, handoff_id: int) -> None:
        if not self.manager_chat_ids:
            log.warning("MANAGER_CHAT_IDS порожній — нікому надіслати заявку #%s", handoff_id)
            return
        for chat_id in self.manager_chat_ids:
            self._spawn(self._send(chat_id, text, claim_keyboard(handoff_id)))

    def send_to_client(self, client_ref: str, text: str) -> None:
        self._spawn(self._send(client_ref, text))

    def send_to_manager(self, manager_ref: str, text: str) -> None:
        self._spawn(self._send(manager_ref, text))


# --------------------------------------------------------------------------
#  Бот
# --------------------------------------------------------------------------
class DeliveryBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo = Repository(settings.path(settings.db_path))
        self.service = OrderService(settings, repo=self.repo)
        self.bot = Bot(settings.telegram_bot_token)
        self.notifier = TelegramNotifier(self.bot, settings.manager_chat_ids)
        self.manager = ManagerService(self.repo, settings, self.notifier)
        self.dp = Dispatcher()
        self._register()

    # ---------------- службове ----------------
    def is_manager(self, chat_id: int) -> bool:
        return chat_id in self.settings.manager_chat_ids

    def _session(self, user_id: int):
        return self.repo.find_active_session("telegram", str(user_id))

    async def _ask(self, message: Message, session, question: Question | None) -> None:
        if question is None:
            await self._finish(message, session)
            return
        progress = self.service.progress(session)
        block = self.service.questionnaire.block_of(question)
        header = f"<b>{block.title}</b>\n{progress.bar()} {progress.percent}%"
        body = [header, "", f"<b>{question.text}</b>"]
        if question.help:
            body.append(f"<i>{question.help}</i>")
        if question.placeholder:
            body.append(f"Напр.: {question.placeholder}")
        if question.type not in {"choice", "bool"} and not question.required:
            body.append("Можна пропустити кнопкою нижче.")
        await message.answer(
            "\n".join(body),
            reply_markup=question_keyboard(question),
            parse_mode=ParseMode.HTML,
        )

    async def _finish(self, message: Message, session) -> None:
        await message.answer("Рахую кілометраж і вартість…")
        result = self.service.quote(session)

        if result.ok:
            text = (
                "<b>Попередній розрахунок</b>\n<pre>"
                + result.quote.as_text()
                + "</pre>\n\nЦіна орієнтовна; фінальну підтверджує менеджер."
            )
            await message.answer(text, reply_markup=quote_keyboard(), parse_mode=ParseMode.HTML)
            if self.settings.orders_chat_id and result.order:
                self.notifier.send_to_manager(
                    str(self.settings.orders_chat_id), result.order.summary()
                )
        else:
            await message.answer(f"⚠ {result.error}", reply_markup=quote_keyboard())

        if session.answers.get("manager_call") or result.needs_manager:
            await self._request_manager(message, session, result.order,
                                        reason="запит у анкеті")

    async def _request_manager(self, message: Message, session, order=None,
                               reason: str = "кнопка «Підключити менеджера»") -> None:
        handoff = self.manager.request(
            session_id=session.session_id if session else "—",
            channel="telegram",
            client_ref=str(message.chat.id),
            client_name=str((session.answers if session else {}).get("client_name") or
                            message.from_user.full_name),
            client_phone=str((session.answers if session else {}).get("client_phone") or ""),
            order=order,
            reason=reason,
        )
        await message.answer(f"👤 {handoff.message}\nПишіть сюди — менеджер прочитає.")

    # ---------------- реєстрація хендлерів ----------------
    def _register(self) -> None:
        dp = self.dp

        @dp.message(CommandStart())
        async def start(message: Message) -> None:
            session, question = self.service.start(
                channel="telegram", user_id=str(message.from_user.id), resume=False
            )
            await message.answer(
                "Вітаю! Я порахую вартість доставки за 5 короткими блоками питань "
                "(2–3 хвилини).\n/manager — одразу покликати менеджера."
            )
            await self._ask(message, session, question)

        @dp.message(Command("help"))
        async def help_cmd(message: Message) -> None:
            lines = [
                "/start — новий розрахунок",
                "/restart — почати анкету заново",
                "/manager — покликати менеджера",
            ]
            if self.is_manager(message.chat.id):
                lines += [
                    "",
                    "Менеджеру:",
                    "/queue — черга запитів",
                    "/take <id> — взяти запит",
                    "/close — завершити розмову",
                    "/orders — останні заявки",
                    "/stats — статистика",
                    "/reload — перечитати тарифи",
                ]
            await message.answer("\n".join(lines))

        @dp.message(Command("restart"))
        async def restart(message: Message) -> None:
            session = self._session(message.from_user.id)
            if session is None:
                session, question = self.service.start(
                    channel="telegram", user_id=str(message.from_user.id), resume=False
                )
            else:
                question = self.service.restart(session)
            await self._ask(message, session, question)

        @dp.message(Command("manager"))
        async def manager_cmd(message: Message) -> None:
            session = self._session(message.from_user.id)
            await self._request_manager(message, session, reason="команда /manager")

        # ---------- команди менеджера ----------
        @dp.message(Command("queue"))
        async def queue(message: Message) -> None:
            if not self.is_manager(message.chat.id):
                return
            waiting = self.manager.waiting()
            if not waiting:
                await message.answer("Черга порожня.")
                return
            for item in waiting:
                await message.answer(
                    f"#{item['id']} · {item['client_name'] or '—'} · "
                    f"{item['client_phone'] or '—'} · {item['created_at']}",
                    reply_markup=claim_keyboard(item["id"]),
                )

        @dp.message(Command("take"))
        async def take(message: Message) -> None:
            if not self.is_manager(message.chat.id):
                return
            parts = (message.text or "").split()
            if len(parts) < 2 or not parts[1].isdigit():
                await message.answer("Використання: /take <номер запиту>")
                return
            result = self.manager.claim(int(parts[1]), str(message.chat.id))
            await message.answer(result.message)

        @dp.message(Command("close"))
        async def close(message: Message) -> None:
            active = self.repo.active_handoff_for_manager(str(message.chat.id))
            if active:
                self.manager.close(active["id"], by="manager")
                return
            own = self.repo.active_handoff_for_client(str(message.chat.id))
            if own:
                self.manager.close(own["id"], by="client")
                await message.answer("Розмову завершено.")
                return
            await message.answer("Активних розмов немає.")

        @dp.message(Command("orders"))
        async def orders(message: Message) -> None:
            if not self.is_manager(message.chat.id):
                return
            rows = self.repo.recent_orders(limit=10)
            if not rows:
                await message.answer("Заявок ще немає.")
                return
            lines = []
            for row in rows:
                answers = row["answers"]
                total = f"{row['total']:,.0f}".replace(",", " ") if row["total"] else "—"
                lines.append(
                    f"#{row['id']} {answers.get('pickup_city', '?')}→"
                    f"{answers.get('dropoff_city', '?')} · {total} · {row['status']}"
                )
            await message.answer("\n".join(lines))

        @dp.message(Command("stats"))
        async def stats(message: Message) -> None:
            if not self.is_manager(message.chat.id):
                return
            data = self.repo.stats()
            await message.answer(
                f"Заявок усього: {data['orders_total']}\n"
                f"Сьогодні: {data['orders_today']}\n"
                f"Середній чек: {data['average_total']:,.0f}".replace(",", " ")
                + f"\nУ черзі на менеджера: {data['handoffs_waiting']}"
            )

        @dp.message(Command("reload"))
        async def reload(message: Message) -> None:
            if not self.is_manager(message.chat.id):
                return
            try:
                tariff = self.service.reload_tariff()
            except Exception as exc:
                await message.answer(f"⚠ Помилка в тарифах: {exc}")
                return
            await message.answer(f"Тарифи перечитано, версія {tariff.version}.")

        # ---------- кнопки ----------
        @dp.callback_query(F.data.startswith(f"{CB_CLAIM}:"))
        async def claim(callback: CallbackQuery) -> None:
            handoff_id = int(callback.data.split(":", 1)[1])
            result = self.manager.claim(handoff_id, str(callback.message.chat.id))
            await callback.answer(result.message, show_alert=not result.ok)
            if result.ok:
                await callback.message.edit_reply_markup(reply_markup=None)

        @dp.callback_query(F.data == CB_MANAGER)
        async def manager_button(callback: CallbackQuery) -> None:
            session = self._session(callback.from_user.id)
            await callback.answer()
            await self._request_manager(callback.message, session)

        @dp.callback_query(F.data == CB_RESTART)
        async def restart_button(callback: CallbackQuery) -> None:
            session, question = self.service.start(
                channel="telegram", user_id=str(callback.from_user.id), resume=False
            )
            await callback.answer()
            await self._ask(callback.message, session, question)

        @dp.callback_query(F.data == CB_BACK)
        async def back_button(callback: CallbackQuery) -> None:
            session = self._session(callback.from_user.id)
            await callback.answer()
            if session is None:
                return
            question = self.service.back(session)
            await self._ask(callback.message, session, question)

        @dp.callback_query(F.data == CB_SKIP)
        async def skip_button(callback: CallbackQuery) -> None:
            session = self._session(callback.from_user.id)
            await callback.answer()
            if session is None:
                return
            result = self.service.answer(session, "-")
            if not result.ok:
                await callback.message.answer(f"⚠ {result.error}")
                return
            await self._ask(callback.message, session, result.question)

        @dp.callback_query(F.data.startswith(f"{CB_ANSWER}:"))
        async def answer_button(callback: CallbackQuery) -> None:
            session = self._session(callback.from_user.id)
            await callback.answer()
            if session is None:
                await callback.message.answer("Сесія застаріла — натисніть /start.")
                return
            question = self.service.engine.current(session)
            if question is None:
                return
            payload = callback.data.split(":", 1)[1]
            if payload in {"yes", "no"}:
                raw: Any = payload == "yes"
            elif payload.isdigit() and int(payload) < len(question.options):
                raw = question.options[int(payload)].value
            else:
                return
            result = self.service.answer(session, raw)
            if not result.ok:
                await callback.message.answer(f"⚠ {result.error}")
                return
            await self._ask(callback.message, session, result.question)

        # ---------- звичайний текст ----------
        @dp.message(F.text)
        async def text(message: Message) -> None:
            chat_id = str(message.chat.id)

            # менеджер у розмові → пересилаємо клієнту
            if self.is_manager(message.chat.id) and self.manager.relay_from_manager(
                chat_id, message.text
            ):
                return
            # клієнт у розмові з менеджером → пересилаємо менеджеру
            if self.manager.relay_from_client(chat_id, message.text):
                return

            session = self._session(message.from_user.id)
            if session is None:
                await message.answer("Щоб почати розрахунок, натисніть /start.")
                return
            result = self.service.answer(session, message.text)
            if not result.ok:
                await message.answer(f"⚠ {result.error}")
                question = self.service.engine.current(session)
                await self._ask(message, session, question)
                return
            await self._ask(message, session, result.question)

    async def run(self) -> None:
        me = await self.bot.get_me()
        log.info("Бот @%s запущено. Менеджери: %s", me.username,
                 self.settings.manager_chat_ids or "не задані")
        await self.dp.start_polling(self.bot)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    if not settings.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN не заданий. Створіть бота у @BotFather "
            "і впишіть токен у .env"
        )
    if not settings.manager_chat_ids:
        log.warning(
            "MANAGER_CHAT_IDS порожній — запити на менеджера нікуди не надходитимуть."
        )
    asyncio.run(DeliveryBot(settings).run())


if __name__ == "__main__":
    main()
