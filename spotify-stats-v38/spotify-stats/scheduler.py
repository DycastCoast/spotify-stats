"""
The built-in alarm clock, for the cluster: checks Spotify for everyone's new plays every
POLL_MINUTES and then warms the main pages, in a background thread inside the website.

On PythonAnywhere this had to be an outside "ping" (ping_site.py) because the free plan
has one worker and no scheduled tasks. In the cluster the site runs with several threads,
so a background thread can do it, and it shares the website's memory, so the pages it
warms are the ones visitors get.

Only starts when RUN_SCHEDULER=1 (set in k8s/deployment.yaml), so running the site on
your PC doesn't poll in the background unless you ask it to.
"""
import logging
import os
import threading
import time

log = logging.getLogger("scheduler")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", 30))
_started = False


def run_once():
    import poller
    import warm
    try:
        log.info("poll: %s", poller.poll_all())
    except Exception:
        log.exception("poll failed")           # one bad run mustn't stop the clock
    try:
        log.info("warm: %ss", warm.warm()["seconds"])
    except Exception:
        log.exception("warm failed")
    try:
        import diskcache
        swept = diskcache.sweep()              # saved pages nobody's asked for in a month
        if swept:
            log.info("cache: removed %s old files", swept)
    except Exception:
        log.exception("cache sweep failed")


def loop():
    time.sleep(30)                             # let the site finish starting first
    while True:
        run_once()
        time.sleep(POLL_MINUTES * 60)


def start():
    global _started
    if os.environ.get("RUN_SCHEDULER") != "1" or _started:
        return
    _started = True
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    threading.Thread(target=loop, name="scheduler", daemon=True).start()
    log.info("scheduler started: polling every %s minutes", POLL_MINUTES)
