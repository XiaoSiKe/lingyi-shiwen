"""节流层：让流量看起来像「人在刷公众号」，而不是脚本在打点。

三个部件：
- `TokenBucket`：出口维度的硬上限（每个 IP 每秒最多几个请求）；
- `AdaptivePacer`：请求间隔 = base * factor，factor 按风控反馈 AIMD 调整
  （命中一次风控 -> 间隔翻倍并降到最低速率；连续成功 N 次 -> 缓慢回收），
  间隔再叠加泊松抖动，避免等距节奏这个最明显的机器特征；
- `CircuitBreaker`：连续风控达到阈值就整体暂停（这时换什么都没用，只能等）。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


class TokenBucket:
    """线程安全的令牌桶，用于单出口 RPS 限制。"""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(1e-4, float(rate))
        self.burst = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.burst
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._ts) * self.rate)
        self._ts = now

    def take(self, tokens: float = 1.0) -> float:
        """返回需要等待的秒数（调用方 sleep）。"""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            need = tokens - self._tokens
            self._tokens = 0.0
            self._ts = time.monotonic()
            return need / self.rate


@dataclass
class AdaptivePacer:
    base_interval: float = 3.2
    jitter_ratio: float = 0.55
    min_factor: float = 0.35
    max_factor: float = 12.0
    recover_after: int = 8
    rng: random.Random = field(default_factory=random.Random)

    _factor: float = 1.0
    _streak: int = 0
    _last: float = 0.0

    @property
    def interval(self) -> float:
        return max(0.2, self.base_interval * self._factor)

    def next_delay(self) -> float:
        """下一次请求前应等待的秒数（指数分布抖动，均值≈interval）。"""
        base = self.interval
        # 泊松到达取指数间隔；限制在 [0.4x, 3x] 防止极端值
        jittered = self.rng.expovariate(1.0 / base) if base > 0 else 0.0
        jittered = min(max(jittered, base * (1 - self.jitter_ratio)), base * (1 + 2 * self.jitter_ratio))
        # 再叠加一个 5% 的「人类停顿」
        if self.rng.random() < 0.05:
            jittered += self.rng.uniform(1.5, 6.0)
        return jittered

    def wait(self) -> float:
        delay = self.next_delay()
        throttle = delay - (time.monotonic() - self._last)
        if throttle > 0:
            time.sleep(throttle)
        self._last = time.monotonic()
        return max(0.0, throttle)

    def on_success(self) -> None:
        self._streak += 1
        if self._streak >= self.recover_after and self._factor > self.min_factor:
            self._factor = max(self.min_factor, self._factor * 0.85)
            self._streak = 0

    def on_risk(self, severity: float = 2.0) -> None:
        self._factor = min(self.max_factor, self._factor * max(1.2, severity))
        self._streak = 0

    def on_hard_block(self) -> None:
        self._factor = self.max_factor
        self._streak = 0

    def snapshot(self) -> dict:
        return {"factor": round(self._factor, 3), "interval": round(self.interval, 2)}


class CircuitBreaker:
    """连续风控熔断：开闸后整段时间不再发请求。"""

    def __init__(self, threshold: int = 5, open_seconds: float = 600.0, rng: random.Random | None = None):
        self.threshold = max(1, int(threshold))
        self.open_seconds = float(open_seconds)
        self._rng = rng or random.Random()
        self._fails = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        with self._lock:
            left = self._open_until - time.monotonic()
            return (left <= 0), max(0.0, left)

    def success(self) -> None:
        with self._lock:
            self._fails = 0

    def failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= self.threshold:
                # 抖动 0.6x~1.4x，避免固定间隔「准点重连」
                self._open_until = time.monotonic() + self.open_seconds * self._rng.uniform(0.6, 1.4)
                self._fails = 0

    def reset(self) -> None:
        with self._lock:
            self._fails = 0
            self._open_until = 0.0
