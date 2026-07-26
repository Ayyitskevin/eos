"""In-process scheduler — drains durable outbound work."""

import logging
import threading
import time

from . import commerce, config, delivery_notify, sequences, webhooks

log = logging.getLogger("eos.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None


_tick = 0


def _loop() -> None:
    global _tick
    while not _stop.wait(config.SEQUENCE_TICK_SECONDS):
        for label, drain in (
            ("sequence", sequences.process_due),
            ("webhook", webhooks.process_pending),
            ("delivery notification", delivery_notify.process_pending),
            ("booking payment reconciliation", commerce.expire_all_pending_bookings),
        ):
            try:
                n = drain()
                if n:
                    log.info("%s scheduler completed %d deliveries", label, n)
            except Exception:
                log.exception("%s sweep failed", label)
        _tick += 1
        if _tick * config.SEQUENCE_TICK_SECONDS >= config.INTEGRATION_TICK_SECONDS:
            _tick = 0
            try:
                from . import jobs, sms

                bucket = int(time.time() // max(config.INTEGRATION_TICK_SECONDS, 1))
                jobs.enqueue(
                    "integration_sweep",
                    {},
                    idempotency_key=f"integration_sweep:{bucket}",
                )
                sms.shoot_day_reminders()
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
