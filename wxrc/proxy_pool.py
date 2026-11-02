"""出口池：代理节点打分 + 粘性绑定 + 冷却。

风控里最有价值的维度是「IP 段」：同一 C 段连续被验证码拦，换号也没用。
所以节点要带 score/冷却，且按「最近最少用 + 分高」挑，避免可预测的轮询顺序
（固定 round-robin 本身也是特征）。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from .security import redact


@dataclass
class ProxyNode:
    url: str                     # "" 表示直连
    label: str = ""
    score: float = 1.0
    ok: int = 0
    fail: int = 0
    captcha: int = 0
    cool_until: float = 0.0
    last_used: float = 0.0

    @property
    def name(self) -> str:
        return self.label or (self.url or "direct")

    def available(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.cool_until


@dataclass
class ProxyPool:
    nodes: list[ProxyNode] = field(default_factory=list)
    max_score: float = 1.0
    min_score: float = 0.05
    cool_rate: float = 900.0        # 命中频率风控
    cool_captcha: float = 1800.0    # 命中验证码，同 IP 基本废一轮
    rng: random.Random = field(default_factory=random.Random)

    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.nodes:
            self.nodes = [ProxyNode(url="", label="direct")]

    # ---------------------------------------------------------------- #
    def acquire(self, exclude: set[str] | None = None) -> ProxyNode | None:
        """挑一个可用节点：分数高优先，同分随机（带权重），并排除本次已试过的。"""
        exclude = exclude or set()
        now = time.time()
        with self._lock:
            cands = [n for n in self.nodes if n.available(now) and n.name not in exclude and n.score > self.min_score]
            if not cands:
                return None
            weights = [max(0.01, n.score) ** 2 for n in cands]
            node = self.rng.choices(cands, weights=weights, k=1)[0]
            node.last_used = now
            return node

    def report(self, node: ProxyNode, *, ok: bool, risk: str = "") -> None:
        now = time.time()
        with self._lock:
            if ok:
                node.ok += 1
                node.score = min(self.max_score, node.score * 1.08 + 0.02)
                return
            node.fail += 1
            if risk == "captcha":
                node.captcha += 1
                node.score *= 0.45
                node.cool_until = now + self.cool_captcha * self.rng.uniform(0.8, 1.5)
            elif risk == "rate":
                node.score *= 0.7
                node.cool_until = now + self.cool_rate * self.rng.uniform(0.6, 1.2)
            else:
                node.score *= 0.9
                node.cool_until = now + 60.0
            node.score = max(self.min_score / 2, node.score)

    def node_for(self, url: str) -> ProxyNode:
        """按 url 取回已绑定节点；池里没有（被删/冷却中）就造一个同 url 的临时节点。"""
        for n in self.nodes:
            if n.url == url:
                return n
        return ProxyNode(url=url, label=(url or "direct"))

    def snapshot(self) -> list[dict]:
        now = time.time()
        return [
            {
                "node": redact(n.name),
                "score": round(n.score, 3),
                "ok": n.ok,
                "fail": n.fail,
                "captcha": n.captcha,
                "cool_left": int(max(0.0, n.cool_until - now)),
            }
            for n in self.nodes
        ]

    def cooldown_all(self, seconds: float) -> None:
        until = time.time() + seconds
        with self._lock:
            for n in self.nodes:
                n.cool_until = max(n.cool_until, until)
