class Account:
    """Счёт трейдера с реализованным и плавающим результатом."""

    def __init__(self, initial_deposit: float):
        self.initial_deposit = float(initial_deposit)
        self.realized_total = 0.0
        self.realized_cycle = 0.0
        self.floating = 0.0

    @property
    def balance(self) -> float:
        return self.initial_deposit + self.realized_total

    @property
    def equity(self) -> float:
        return self.balance + self.floating

    def realize(self, pnl: float) -> None:
        self.realized_cycle += pnl
        self.realized_total += pnl

    def set_floating_pnl(self, pnl: float) -> None:
        self.floating = float(pnl)

    def clear(self) -> None:
        self.realized_cycle = 0.0
        self.floating = 0.0

    def snapshot(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "realized_cycle": round(self.realized_cycle, 2),
        }
