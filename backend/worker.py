"""Background job runner.

Runs the APScheduler jobs (limit-order matching and order expiry) in a
dedicated process. Keeping them out of the web containers means the jobs
execute exactly once per interval no matter how many gunicorn workers are
serving traffic.

    python -m backend.worker
"""

import logging
import signal
import threading

from . import config
from .app import expire_old_orders, process_pending_limit_orders, scheduler

logger = logging.getLogger(__name__)

_shutdown = threading.Event()


def _handle_signal(signum, _frame):
    logger.info("Received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    scheduler.add_job(process_pending_limit_orders, "interval", seconds=30,
                      id="process_limits", replace_existing=True)
    scheduler.add_job(expire_old_orders, "interval", minutes=5,
                      id="expire_orders", replace_existing=True)
    scheduler.start()
    logger.info("Worker started: limit matching every 30s, expiry every 5m")

    _shutdown.wait()
    scheduler.shutdown(wait=True)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
