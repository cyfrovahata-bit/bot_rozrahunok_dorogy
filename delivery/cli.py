"""Консольний режим — перевірити анкету й тарифи без Telegram.

    python -m delivery.cli                 # пройти анкету вручну
    python -m delivery.cli --demo          # прогнати приклад заявки
    python -m delivery.cli --distance Київ Львів
    python -m delivery.cli --orders        # останні заявки
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .config import Settings
from .geo.base import GeoError
from .questionnaire import render_question
from .services.order_service import OrderService
from .services.manager import ManagerService

LINE = "─" * 56

DEMO_ANSWERS: dict[str, Any] = {
    "client_name": "Олександр",
    "client_phone": "+380671112233",
    "client_type": "company",
    "company_name": "ТОВ «Ромашка»",
    "pickup_city": "Київ",
    "pickup_address": "вул. Хрещатик, 22",
    "dropoff_city": "Львів",
    "dropoff_address": "вул. Городоцька, 10",
    "extra_stops": 1,
    "return_trip": False,
    "cargo_type": "fragile",
    "weight_kg": 1200,
    "volume_m3": 6,
    "packages": 4,
    "declared_value": 250000,
    "vehicle_type": "truck_5t",
    "temperature_mode": "none",
    "loaders": 2,
    "tail_lift": True,
    "urgency": "next_day",
    "pickup_date": "2026-10-05",
    "payment_method": "invoice",
    "vat_invoice": True,
    "insurance": True,
    "comment": "Завантаження з рампи, пропуск замовляти за добу.",
    "manager_call": True,
}


def run_interactive(service: OrderService) -> int:
    session, question = service.start(channel="cli")
    print(LINE)
    print(service.questionnaire.title)
    print("Команди: «назад» — попереднє питання, «вихід» — завершити.")
    print(LINE)

    shown_blocks: set[str] = set()
    while question is not None:
        block = service.questionnaire.block_of(question)
        if block.id not in shown_blocks:
            shown_blocks.add(block.id)
            print(f"\n=== {block.title} ===")
            if block.description:
                print(block.description)
        print()
        print(render_question(question, service.progress(session)))
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПерервано.")
            return 1

        if raw.lower() in {"вихід", "exit", "quit"}:
            print("Анкету збережено, повернетесь пізніше.")
            return 0
        if raw.lower() in {"назад", "back"}:
            question = service.back(session)
            continue

        result = service.answer(session, raw)
        if not result.ok:
            print(f"⚠ {result.error}")
            continue
        question = result.question

    return finish(service, session)


def finish(service: OrderService, session) -> int:
    print("\n" + LINE)
    print("Перевірте дані:")
    for text, value in service.summary_lines(session):
        print(f"  {text}: {value}")

    print("\nРахую кілометраж і вартість...")
    result = service.quote(session)
    print(LINE)
    if not result.ok:
        print(f"⚠ {result.error}")
    else:
        print(result.quote.as_text())
        print(LINE)
        print(f"Заявка #{result.order.id} збережена.")

    if session.answers.get("manager_call") or result.needs_manager:
        manager = ManagerService(service.repo, service.settings)
        handoff = manager.request(
            session_id=session.session_id,
            channel="cli",
            client_ref=session.session_id,
            client_name=str(session.answers.get("client_name") or ""),
            client_phone=str(session.answers.get("client_phone") or ""),
            order=result.order,
            reason="запит клієнта в анкеті" if session.answers.get("manager_call") else "збій розрахунку",
        )
        print(f"\n👤 {handoff.message} (запит #{handoff.handoff_id})")
    return 0


def run_demo(service: OrderService) -> int:
    session = service.engine.start(channel="cli")
    session.answers.update(DEMO_ANSWERS)
    session.cursor = len(service.questionnaire.questions)
    service.repo.save_session(session)
    print(LINE)
    print("ДЕМО-ЗАЯВКА")
    return finish(service, session)


def run_distance(service: OrderService, points: list[str]) -> int:
    try:
        info = service.geo.route(points)
    except GeoError as exc:
        print(f"⚠ {exc}")
        return 1
    print(f"{' → '.join(points)}")
    print(f"Відстань: {info.distance_km:.0f} км, у дорозі ~{info.duration_min / 60:.1f} год")
    print(f"Джерело: {info.provider}" + (" (кеш)" if info.from_cache else ""))
    if info.note:
        print(info.note)
    return 0


def run_orders(service: OrderService) -> int:
    orders = service.repo.recent_orders(limit=15)
    if not orders:
        print("Заявок ще немає.")
        return 0
    print(f"{'#':>4}  {'дата':<20} {'маршрут':<32} {'сума':>10}  статус")
    for order in orders:
        answers = order["answers"]
        route = f"{answers.get('pickup_city', '?')} → {answers.get('dropoff_city', '?')}"
        total = f"{order['total']:,.0f}".replace(",", " ") if order["total"] else "—"
        print(f"{order['id']:>4}  {order['created_at']:<20} {route:<32} {total:>10}  {order['status']}")
    stats = service.repo.stats()
    print(f"\nУсього: {stats['orders_total']}, сьогодні: {stats['orders_today']}, "
          f"середній чек: {stats['average_total']:,.0f}".replace(",", " "))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Розрахунок вартості доставки")
    parser.add_argument("--demo", action="store_true", help="прогнати приклад заявки")
    parser.add_argument("--distance", nargs="+", metavar="ТОЧКА",
                        help="порахувати лише кілометраж між точками")
    parser.add_argument("--orders", action="store_true", help="показати останні заявки")
    parser.add_argument("--env", default=".env", help="шлях до .env")
    args = parser.parse_args(argv)

    settings = Settings.load(args.env)
    service = OrderService(settings)

    if args.distance:
        return run_distance(service, args.distance)
    if args.orders:
        return run_orders(service)
    if args.demo:
        return run_demo(service)
    return run_interactive(service)


if __name__ == "__main__":
    sys.exit(main())
