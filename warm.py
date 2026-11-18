"""Work out everyone's main pages in advance, so visitors don't wait.

Every page's results are saved in memory until that person gets new plays. New
plays arrive every 30 minutes (the alarm clock), so without this nearly every
visit would be the "first visit" that has to work everything out (several seconds,
more on a small free server). Called by /internal/poll right after polling, when
nobody is waiting. Anything already worked out returns instantly.
"""
import logging
import time

log = logging.getLogger(__name__)
TIME_LIMIT_S = 240          # stop starting new pages after this, so the request never runs away


def warm():
    import compare, journey, now, playlists, skips, stats
    started, done = time.time(), []
    people = [p["id"] for p in stats.people()]
    jobs = []
    for p in people:
        jobs += [("now", lambda p=p: now.right_now(p)),
                 ("overview", lambda p=p: stats.overview(p)),
                 ("found you", lambda p=p: stats.discovery(p, None, stats.latest_play_id(p))),
                 ("journey", lambda p=p: journey.journey(p, "broad")),
                 ("skips", lambda p=p: skips.skips(p)),
                 ("moods", lambda p=p: playlists.moods(p, 2))]
    for a in people:
        for b in people:
            if a != b:
                jobs.append(("compare", lambda a=a, b=b: compare.compare(a, b)))
    for name, job in jobs:
        if time.time() - started > TIME_LIMIT_S:
            break
        t = time.time()
        try:
            job()
        except Exception:
            log.exception("warming %s failed", name)       # a broken page shouldn't stop the rest
        done.append((name, round(time.time() - t, 1)))
    return {"seconds": round(time.time() - started, 1), "pages": done}
