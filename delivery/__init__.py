"""Розрахунок вартості доставки: анкета, кілометраж, прайсинг, менеджер.

Ядро (questionnaire, geo, pricing, storage, services) не залежить від
Telegram і працює на стандартній бібліотеці Python.
Адаптери (delivery.bot.telegram, delivery.api.app) підключаються зверху.
"""

__version__ = "1.0.0"
