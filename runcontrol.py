"""
Coöperatieve pauzeer/hervat/stop-besturing, gedeeld door fetcher en classifier.

De UI-thread zet de vlaggen; de async workers controleren ze vóór elke nieuwe
taak. Lopende requests ronden altijd af (en worden weggeschreven), nieuwe
starten pas na hervatten — of niet meer na stoppen.
"""
from __future__ import annotations

import asyncio
import threading


class RunControl:
    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._cancel = threading.Event()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()   # wachtende taken wakker maken zodat ze kunnen stoppen

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    async def checkpoint(self) -> bool:
        """Wacht zolang gepauzeerd. Geeft False terug als er gestopt werd."""
        while not self._running.is_set():
            await asyncio.sleep(0.2)
        return not self._cancel.is_set()
