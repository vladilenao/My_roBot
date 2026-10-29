# Подложенные wheel-зависимости

`t-tech-investments` (T-Invest Python SDK, Apache-2.0) публикуется только на
индексе `opensource.tbank.ru`, который недоступен с GitHub Actions. Поэтому
workflow `build-release.yml` ставит SDK из wheel в этой папке.

Версия wheel обязана совпадать с `t-tech-investments==X.Y.Z` в
`requirements.txt`; иначе CI остановится на шаге подготовки зависимостей.
Порядок обновления — `docs/release/build-and-distribution.md`.
