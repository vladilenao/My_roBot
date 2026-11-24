"""Контур на брокере (add-sandbox-contour): допуск, отпечаток счёта, сверщик, шаг процесса.

Общий код не знает брокера: адаптер (``tinvest/``, на Ф2–Ф3 ``finam/``) передаёт точка сборки
``run.py`` по описанию контура (``contours.py``, refactor-broker-port). Виды: ``sandbox`` — песочница
Т-Инвест, ``account`` — брокерский счёт робота (add-account-contour).
"""

from src.broker_contour.contours import ACCOUNT, SANDBOX  # noqa: F401
