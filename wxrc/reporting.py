"""采集范围与停止原因：完成仅针对声明的可见范围。"""
from dataclasses import asdict, dataclass


@dataclass
class Coverage:
    scope: str
    stop_reason: str = 'running'
    pages: int = 0
    coverage_complete: bool = False
    error_type: str = ''

    def stop(self, reason: str, *, complete: bool = False) -> None:
        self.stop_reason = reason
        self.coverage_complete = complete

    def interrupt(self, error: BaseException) -> None:
        self.error_type = type(error).__name__
        reason = {"BreakerOpen": "circuit_open", "NoIdentity": "no_identity",
                  "KeyboardInterrupt": "cancelled", "GeneratorExit": "interrupted"}.get(self.error_type, "error")
        if getattr(error, "expired", False):
            reason = "login_expired"
        elif getattr(error, "rate_limited", False):
            reason = "rate_limited"
        self.stop(reason)

    def as_dict(self) -> dict:
        return asdict(self)
