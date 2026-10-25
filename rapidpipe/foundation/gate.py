"""The quality gate's query: should a packet set go to the topic, or be held?

Plan v9 Part 7, second iteration: an epoch off-scale against its run is
held, not sent, and reported each pass as ``held`` fields are. The gate
itself sits at the send boundary (the packet job's own send and the loop's
recovery sweep, ``rapidpipe.foundation.publish_kafka``); this module only
answers. Given a packet set (its job), :func:`decide` reads the metrics of
the difference job whose source set it names (``job_metrics``) and holds it
when any of:

- ``usable_fraction`` below ``[gate] usable_fraction_min`` (lost sky: a
  reference that covers part of the SCA, non-finite difference pixels);
- the registration offset, ``hypot(registration_dx_px,
  registration_dy_px)``, above ``[gate] registration_max_px`` (a WCS
  offset like HLWAS's 1 px before its overlay's ``crpix_shift``);
- ``n_sources_snr_gt20`` above ``[gate] snr_gt20_factor`` times the
  median over the run's published difference jobs;
- ``core_fraction`` or ``ring_fraction`` beyond ``[gate]
  core_fraction_max_abs`` or ``ring_fraction_max_abs`` in absolute value
  (signed values near zero make a ratio to the median meaningless, so the
  bound is absolute);

the first two named first, since lost sky or a misregistration inflates the
count and the residuals after it; and only once the run has at least
``[gate] min_epochs`` published difference jobs with the count, so a run's
first epochs set its scale before any is held. A metric the difference job could not compute is not a
reason to hold. Nothing is deleted or unbound: a held set stays official and
unsent, and a later pass whose answer is send sends it (the run's median
moves as epochs arrive); there is no release verb.

:func:`judge` is the rule on numbers alone; :func:`decide` reads them.
"""

from __future__ import annotations

import math
from typing import Any

from rapidpipe.foundation.official import NotFound

#: The ``[gate]`` table's defaults, for a run whose body predates it.
DEFAULTS = {"snr_gt20_factor": 3.0, "core_fraction_max_abs": 0.025, "ring_fraction_max_abs": 0.015,
            "min_epochs": 5, "usable_fraction_min": 0.9, "registration_max_px": 0.25}


def judge(metrics: dict[str, float], median: float | None, epochs: int,
          settings: dict[str, Any] | None = None) -> dict:
    """``{"send", "metric", "value", "bound", "reason"}`` for one difference job's ``metrics``.

    ``median`` and ``epochs`` are the run's ``n_sources_snr_gt20`` median and its count
    of published difference jobs carrying it. A send has ``metric``, ``value`` and
    ``bound`` ``None``; a hold names the first metric beyond its bound.
    """
    s = {**DEFAULTS, **(settings or {})}
    send = {"send": True, "metric": None, "value": None, "bound": None, "reason": None}
    if epochs < int(s["min_epochs"]):
        return send
    usable, bound = metrics.get("usable_fraction"), float(s["usable_fraction_min"])
    if usable is not None and usable < bound:
        return {"send": False, "metric": "usable_fraction", "value": usable, "bound": bound,
                "reason": f"usable_fraction {usable:.4f} below {bound:g}"}
    dx, dy = metrics.get("registration_dx_px"), metrics.get("registration_dy_px")
    if dx is not None and dy is not None and math.hypot(dx, dy) > float(s["registration_max_px"]):
        offset, bound = math.hypot(dx, dy), float(s["registration_max_px"])
        return {"send": False, "metric": "registration", "value": offset, "bound": bound,
                "reason": f"registration offset {offset:.3f} px ({dx:+.3f}, {dy:+.3f}) above {bound:g} px"}
    count = metrics.get("n_sources_snr_gt20")
    if count is not None and median is not None and count > float(s["snr_gt20_factor"]) * median:
        bound = float(s["snr_gt20_factor"]) * median
        return {"send": False, "metric": "n_sources_snr_gt20", "value": count, "bound": bound,
                "reason": f"n_sources_snr_gt20 {count:g} above {s['snr_gt20_factor']:g} times the run's"
                          f" median {median:g} over {epochs} epochs"}
    for name in ("core_fraction", "ring_fraction"):
        value, bound = metrics.get(name), float(s[f"{name}_max_abs"])
        if value is not None and abs(value) > bound:
            return {"send": False, "metric": name, "value": value, "bound": bound,
                    "reason": f"{name} {value:+.4f} beyond ±{bound:g}"}
    return send


def decide(conn, packet_job: int, settings: dict[str, Any] | None = None) -> dict:
    """:func:`judge` for the packet set ``packet_job``, read from ``job_metrics`` in one transaction.

    ``settings`` is the run's ``[gate]`` table (its defaults when absent). Adds
    ``difference_job`` to the answer; ``NotFound`` when the set names no source set.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT source_job FROM job_inputs WHERE job = %s AND kind = 'input'"
                    " AND product = 'sources'", (packet_job,))
        row = cur.fetchone()
        if row is None:
            conn.commit()
            raise NotFound(f"packet set {packet_job} names no sources input")
        difference = row[0]
        cur.execute("SELECT metric, value FROM job_metrics WHERE job = %s", (difference,))
        metrics = dict(cur.fetchall())
        cur.execute("SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY m.value)"
                    " FROM job_metrics m JOIN jobs d ON d.run = m.run WHERE d.id = %s"
                    " AND m.stage = 'difference' AND m.state = 'published'"
                    " AND m.metric = 'n_sources_snr_gt20'", (difference,))
        epochs, median = cur.fetchone()
    conn.commit()
    return {**judge(metrics, median, int(epochs), settings), "difference_job": difference}
