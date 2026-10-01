"""Ошибки управления сделкой, отличимые от ошибок программиста.

``ValueError`` в слое управления означает нарушение предусловия вызывающего
кода (чужое действие, устаревшая ревизия, недопустимая фаза) и в ``manage()``
трактуется как «пропустить это действие». Отказ защиты по границе рынка —
штатная отрицательная ветка бизнес-логики, поэтому он выражен отдельным типом:
его нельзя спутать ни с дубликатом команды, ни с ошибкой вызова, а перехват
остаётся достаточно узким, чтобы не превращать отказ в ``admission-error``.
"""


class TradeManagementException(Exception):
    """Базовая ошибка решения системы управления сделкой."""


class InvalidStopBoundaryError(TradeManagementException):
    """Защитный стоп вышел за границу рынка и не может быть применён."""


class ReservationRejected(TradeManagementException):
    """Резервирование входа отклонено конкретным исчерпанным лимитом.

    Несёт код отказа (`risk-budget`, `margin-committed-by-pending-orders` или
    `margin-budget`) и числа отказа из момента проверки: занятый и запрошенный
    кандидатом риск и маржу вместе с соответствующими бюджетами. Числа
    сохраняются здесь потому, что reducer обнуляет суммы резерва при его
    освобождении, и постфактум из базы их уже не восстановить.
    """

    def __init__(
        self,
        code: str,
        *,
        used_risk,
        candidate_risk,
        risk_budget,
        used_margin,
        candidate_margin,
        margin_budget,
    ) -> None:
        self.code = code
        self.used_risk = used_risk
        self.candidate_risk = candidate_risk
        self.risk_budget = risk_budget
        self.used_margin = used_margin
        self.candidate_margin = candidate_margin
        self.margin_budget = margin_budget
        super().__init__(code)