"""Primitivas de comunicação entre threads.

`Latest` tem semântica "só o último valor importa": um consumidor lento (ex.: detector)
nunca fica processando frames velhos — ele sempre pega o mais recente. Isso é o que
mantém a latência baixa no pipeline inteiro.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class Latest(Generic[T]):
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._value: Optional[T] = None
        self._t = 0.0
        self._seq = 0

    def publish(self, value: T, t: float | None = None) -> int:
        with self._cond:
            self._value = value
            self._t = time.monotonic() if t is None else t
            self._seq += 1
            self._cond.notify_all()
            return self._seq

    def get(self) -> tuple[Optional[T], float, int]:
        with self._cond:
            return self._value, self._t, self._seq

    def wait_newer(self, seq: int, timeout: float | None = None) -> Optional[tuple[T, float, int]]:
        """Bloqueia até existir um valor com seq > `seq`. Retorna None no timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > seq, timeout=timeout):
                return None
            return self._value, self._t, self._seq  # type: ignore[return-value]


class RateMeter:
    """Frequência (Hz) numa janela deslizante."""

    def __init__(self, window_s: float = 2.0) -> None:
        self.window_s = window_s
        self._ts: deque[float] = deque()
        self._lock = threading.Lock()

    def tick(self, t: float | None = None) -> None:
        t = time.monotonic() if t is None else t
        with self._lock:
            self._ts.append(t)
            self._trim(t)

    def _trim(self, now: float) -> None:
        while self._ts and now - self._ts[0] > self.window_s:
            self._ts.popleft()

    def hz(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            n = len(self._ts)
            if n < 2:
                return 0.0
            span = self._ts[-1] - self._ts[0]
            return (n - 1) / span if span > 0 else 0.0


class Ema:
    """Média móvel exponencial (para tempos de processamento)."""

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha
        self.value: float | None = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.alpha * (x - self.value)
        return self.value
