"""In-process scheduler — drains due email sequence runs."""

import logging
import threading

from . import config, sequences

log = logging.getLogger("eos.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None


_tick = 0


def _loop() -> None:
    global _tick
    while not _stop.wait(config.SEQUENCE_TICK_SECONDS):
        try:
            n = sequences.process_due()
            if n:
                log.info("sequence scheduler sent %d emails", n)
        except Exception:
            log.exception("sequence sweep failed")
        _tick += 1
        if _tick * config.SEQUENCE_TICK_SECONDS >= config.INTEGRATION_TICK_SECONDS:
            _tick = 0
            try:
                from . import jobs
                jobs.enqueue("integration_sweep", {})
            except Exception:
                log.exception("integration sweep enqueue failed")


def start() -> None:
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="eos-sequences", daemon=True)
    _thread.start()
    log.info("sequence scheduler up (every %ss)", config.SEQUENCE_TICK_SECONDS)


def stop() -> None:
    global _thread
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
        _thread = None