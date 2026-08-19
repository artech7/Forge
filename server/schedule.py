"""Work schedule.

Scanning and queueing always run, so the backlog stays visible. Only the
handing-out of jobs is gated — that way you can see what's waiting for
tonight without it competing with your evening streaming.
"""
import time
from datetime import datetime, time as dtime

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def _parse(hhmm, fallback):
    try:
        hour, _, minute = hhmm.partition(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def window_contains(window, now: datetime):
    start = _parse(window.get("start"), dtime(0, 0))
    end = _parse(window.get("end"), dtime(23, 59))
    days = window.get("days") or list(range(7))
    clock = now.time()

    if start <= end:
        # Same-day window, e.g. 09:00 to 17:00.
        return now.weekday() in days and start <= clock < end

    # Overnight window, e.g. 22:00 to 06:00. The tail belongs to the day
    # the window started on, so Friday 22:00-06:00 covers Saturday 2am.
    if clock >= start:
        return now.weekday() in days
    yesterday = (now.weekday() - 1) % 7
    return clock < end and yesterday in days


def is_open(settings, now=None):
    """True if work may be handed out right now."""
    schedule = settings.get("schedule") or {}
    if not schedule.get("enabled"):
        return True
    windows = schedule.get("windows") or []
    if not windows:
        return True
    now = now or datetime.now()
    return any(window_contains(w, now) for w in windows)


def describe(settings):
    schedule = settings.get("schedule") or {}
    if not schedule.get("enabled"):
        return "Running any time"
    windows = schedule.get("windows") or []
    if not windows:
        return "Running any time"
    parts = []
    for w in windows:
        days = w.get("days") or list(range(7))
        if len(days) == 7:
            label = "Every day"
        elif days == [0, 1, 2, 3, 4]:
            label = "Weekdays"
        elif days == [5, 6]:
            label = "Weekends"
        else:
            label = ", ".join(DAY_NAMES[d][:3] for d in sorted(days))
        parts.append(f"{label} {w.get('start','22:00')}–{w.get('end','06:00')}")
    return "; ".join(parts)


def next_change(settings, now=None):
    """Rough note about when the current state flips, for the UI."""
    now = now or datetime.now()
    if not (settings.get("schedule") or {}).get("enabled"):
        return None
    return "open" if is_open(settings, now) else "closed"


# --------------------------------------------------------- cleanup timing

def cleanup_due(conf, last_run=None, now=None):
    """Is the Originals sweep due?

    Three modes, because people think about this differently: every day at a
    time, only on chosen days at a time, or simply every N hours.
    """
    if not conf.get("enabled"):
        return False, "turned off"
    now = now or datetime.now()
    mode = conf.get("mode", "daily")

    if mode == "interval":
        hours = float(conf.get("interval_hours") or 24)
        if last_run is None:
            return True, "first run"
        elapsed = (now - last_run).total_seconds() / 3600
        if elapsed >= hours:
            return True, f"{elapsed:.0f}h since the last sweep"
        return False, f"next in {hours - elapsed:.1f}h"

    target = _parse(conf.get("run_at"), dtime(3, 0))
    if mode == "days":
        days = conf.get("days") or []
        if now.weekday() not in days:
            return False, "not a scheduled day"

    if now.time() < target:
        return False, f"waiting for {conf.get('run_at', '03:00')}"
    # Once per calendar day, whichever mode.
    if last_run and last_run.date() == now.date():
        return False, "already run today"
    return True, f"due since {conf.get('run_at', '03:00')}"


def describe_cleanup(conf):
    if not conf.get("enabled"):
        return "Originals are only cleared by hand"
    days_text = ""
    mode = conf.get("mode", "daily")
    if mode == "interval":
        hours = int(conf.get("interval_hours") or 24)
        when = "every 24 hours" if hours == 24 else f"every {hours} hours"
    else:
        at = conf.get("run_at", "03:00")
        if mode == "days":
            days = sorted(conf.get("days") or [])
            if len(days) == 7:
                days_text = "every day"
            elif days == [0, 1, 2, 3, 4]:
                days_text = "weekdays"
            elif days == [5, 6]:
                days_text = "weekends"
            elif days:
                days_text = ", ".join(DAY_NAMES[d][:3] for d in days)
            else:
                return "No days selected, so nothing is cleared automatically"
            when = f"{days_text} at {at}"
        else:
            when = f"daily at {at}"
    after = float(conf.get("after_days") or 0)
    kept = "immediately" if after == 0 else (
        "after a day" if after == 1 else f"after {after:g} days")
    return f"Originals cleared {when}, {kept}"


# ------------------------------------------------------- giving up on a job

UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}


def limit_seconds(conf):
    """The configured run limit, in seconds, or None when it's off."""
    if not (conf or {}).get("enabled"):
        return None
    try:
        amount = float(conf.get("amount", 6))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount * UNIT_SECONDS.get(conf.get("unit", "hours"), 3600)


def stall_seconds(conf):
    """How long without progress counts as stuck, or None when it's off."""
    conf = conf or {}
    if not conf.get("enabled") or not conf.get("stall_enabled", True):
        return None
    try:
        minutes = float(conf.get("stall_minutes", 30))
    except (TypeError, ValueError):
        return None
    return minutes * 60 if minutes > 0 else None


def human_duration(seconds):
    """A short, readable span: '45 minutes', '6 hours', '2 days'."""
    if seconds is None:
        return "no limit"
    if seconds < 3600:
        value, unit = seconds / 60, "minute"
    elif seconds < 86400:
        value, unit = seconds / 3600, "hour"
    else:
        value, unit = seconds / 86400, "day"
    rounded = round(value, 1)
    if rounded == int(rounded):
        rounded = int(rounded)
    return f"{rounded} {unit}" + ("" if rounded == 1 else "s")


def overrun_reason(job, conf, now=None):
    """Should this running job be given up on? Returns a reason, or None.

    Only ever applied to work in progress. A job waiting in the queue hasn't
    started, so no amount of waiting should fail it.
    """
    if job.get("state") not in ("leased", "running"):
        return None
    now = now or time.time()

    started = job.get("started_at")
    limit = limit_seconds(conf)
    if limit and started and (now - started) > limit:
        return (f"Ran for {human_duration(now - started)}, longer than the "
                f"limit of {human_duration(limit)}. Given up on "
                f"automatically.")

    stall = stall_seconds(conf)
    if stall and job.get("state") == "running":
        # Falls back to the start time so a job that never reported anything
        # is still caught.
        last = job.get("progress_at") or started
        if last and (now - last) > stall:
            return (f"Made no progress for {human_duration(now - last)}. "
                    f"Given up on automatically.")
    return None


def describe_auto_fail(conf):
    conf = conf or {}
    if not conf.get("enabled"):
        return "Jobs run for as long as they need"
    parts = [f"given up on after {human_duration(limit_seconds(conf))}"]
    stall = stall_seconds(conf)
    if stall:
        parts.append(f"or after {human_duration(stall)} with no progress")
    return "Jobs " + ", ".join(parts)
