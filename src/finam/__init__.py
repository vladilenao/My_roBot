"""Разведка Finam Trade API (add-finam-probe): команда ``python -m src.finam``."""

import os

# Прогон 25.11: c-ares (резолвер gRPC по умолчанию) сам спрашивает DNS-серверы из настроек сети и падал
# с «Timeout while contacting DNS servers», хотя система находила api.finam.ru. Системный резолвер
# (getaddrinfo) работает как остальные программы. Задаётся до первого импорта grpc; явное значение
# в окружении не трогаем.
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")
