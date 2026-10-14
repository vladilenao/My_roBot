# Redis Stream market.events

Свечные паттерны публикуются, когда в `robot.toml` включен раздел:

```toml
[market_structure]
patterns_enabled = true  # Включает публикацию паттернов в live-режиме.
redis_host = "127.0.0.1"  # Имя хоста или IP-адрес Redis.
redis_port = 6379  # TCP-порт Redis.
redis_db = 0  # Логическая база Redis для stream market.events.
queue_maxsize = 1024  # Максимум задач, ожидающих фонового анализа.
retention_days = 90  # Срок хранения событий в stream в сутках.
```

Пароль, если Redis требует аутентификацию, задаётся только переменной окружения
`MARKET_STRUCTURE_REDIS_PASSWORD`.

Машиночитаемый контракт сообщений приведён в
[`market-events.asyncapi.yaml`](market-events.asyncapi.yaml).

На сервере Redis требуется AOF с `appendfsync always`. Worker записывает
паттерн в SQLite outbox до `XADD`, а затем повторяет недоставленные сообщения.
Каждый независимый потребитель создаёт собственную Consumer Group с `0-0
MKSTREAM`, после рестарта читает свои pending entries с `0`, новые — с `>`, и
делает `XACK` только после идемпотентной фиксации по `event_id`.

Не реже одного раза за сутки worker применяет точный `XTRIM MINID` от времени
Redis минус срок хранения. Pending entries старше срока могут потерять payload.
