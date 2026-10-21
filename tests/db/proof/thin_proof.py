"""The procedure proof on the thin field: Part 3's thirteen steps through the verbs (foundation m3).

Foundation plan v5, Part 3, on the real data of milestone 1: field 4658155's
two epochs (the IMSS socsimsdb 20260821 run, SCA 8, W146). Not a test
module and not collected by CI, because the field's files stay on the host
that holds them. It creates a fresh database with ``database/schema.sql``
and runs every step in order, through ``rapidpipe run create``, ``official
change``, ``official undo``, ``job local``, ``job run`` and
``scripts/read_packets.py`` only, then asserts each step's observation as
Part 3 states it and prints one line per step::

    STEP <n> PASS|FAIL <observation>

Usage, with ``PG*`` naming the server (not the database: the proof creates
its own and refuses one that exists)::

    python tests/db/proof/thin_proof.py --data-dir DIR --outputs-root file:///.../ \\
        --database NAME --scratch DIR

``--scratch`` holds the two git worktrees of step 9, removed at the end.
Every job writes under ``<outputs root>/<run>/<stage>/<job>/<attempt>/``.
Exit 0 when all thirteen pass.

What the steps need that Part 3 does not say, chosen here:

- The recipes. A is the thin settings body as committed, live first. B
  (step 3) changes the source selection (``min_snr`` 3) and widens the match
  radius to 30 arcseconds, so the field's objects gain members across
  epochs and step 7's trigger bound is not vacuous (at dev's radius no
  epoch-2 source of this field matches an epoch-1 object). C is step 7's
  correction (B's body). P and Q are step 9's two people. D is step 12's
  recipe, E step 13's; both carry A's body.
- Step 2's candidates read set 1 and associate epoch 2 again, both expecting
  set 2's binding: the field has two epochs, so no epoch follows set 2.
- Step 5's late job is launched (its job row, inputs and expected binding
  recorded) before step 4's cutover commits and publishes after it; the
  proof starts it, waits until it is running, then applies the cutover.
- Steps 10 and 11 hold one side's group open on a connection with
  ``rapidpipe.foundation.apply`` (no verb holds a transaction open); the
  other side is the verb, in its own process.
- Step 13's replacement import gives the epoch-1 L2 and difference rows new
  ids (the source system's plus 10^9): ``rid`` and ``pid`` are primary
  keys. It expects only the difference image and source set bindings, so
  its exposure and L2 rows are published unbound.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import fastavro
import psycopg2

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from rapidpipe.cli import main as cli  # noqa: E402
from rapidpipe.foundation import apply  # noqa: E402
from rapidpipe.foundation.local import local_path  # noqa: E402
from tests.db.test_queries import load_queries  # noqa: E402

SURVEY, DATASET, FIELD, SCA, FID = "GBTDS", "socsims-fakesrc-20260807", 4658155, 8, 8
OKEY = {"dataset": DATASET, "survey": SURVEY, "field": FIELD}
#: The field's two epochs, as the source system holds them (FIELD-MANIFEST rows).
EPOCHS = {
    1: {"expid": 84613, "rid": 1832705, "pid": 350875, "rfid": 90011,
        "l2": "l2/r0034001004001029016_0001_wfi08_f146_cal_lite.fits.gz",
        "difference": "jid143931"},
    2: {"expid": 84624, "rid": 1832552, "pid": 351878, "rfid": 90011,
        "l2": "l2/r0034001004001029022_0001_wfi08_f146_cal_lite.fits.gz",
        "difference": "jid144961"},
}
SHARED = {"reference": {"rfid": 90011, "dir": "jid129279"},
          "psf": {"psfid": 153, "kind": "science", "path": "psf/sciimage_psf_f146_sca08.fits"},
          "gaia": {"name": "gaia", "version": "DR3",
                   "path": "gaia/gaia-dr3-field-4658155-r0.12deg.parquet"}}
PSF_KEY = {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "science"}
REF_KEY = {"dataset": DATASET, "field": FIELD, "fid": FID}
GAIA_KEY = {"name": "gaia", "version": "DR3"}
REIMPORT_ID_OFFSET = 10 ** 9
WHO = "thin-proof"
QUERIES = load_queries()


def ikey(epoch: int) -> dict:
    """An epoch's image key: its L2, difference image, source set and packet set."""
    return {"dataset": DATASET, "expid": EPOCHS[epoch]["expid"], "sca": SCA, "fid": FID}


class Failed(Exception):
    """A step's observation did not hold."""


def check(condition: bool, what: str) -> None:
    if not condition:
        raise Failed(what)


class Proof:
    def __init__(self, args):
        self.args = args
        self.env = {**os.environ, "PGDATABASE": args.database,
                    "RAPIDPIPE_OUTPUTS_ROOT": args.outputs_root, "PYTHONPATH": str(REPO)}
        os.environ.update(PGDATABASE=args.database, RAPIDPIPE_OUTPUTS_ROOT=args.outputs_root)
        self.db = psycopg2.connect(dbname=args.database, application_name="thin-proof")
        self.db.autocommit = True
        self.tmp = Path(tempfile.mkdtemp(prefix="thin-proof-"))
        self.settings = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
        self.revision = self.git("rev-parse", "HEAD")
        self.imports: dict = {}
        self.made: dict = {}

    # -- plumbing ------------------------------------------------------------

    def git(self, *argv, cwd: Path = REPO) -> str:
        return subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True,
                              text=True).stdout.strip()

    def rows(self, query: str, params=()) -> list[tuple]:
        with self.db.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []

    def one(self, query: str, params=()):
        rows = self.rows(query, params)
        return rows[0][0] if rows else None

    def query(self, name: str, **params) -> list[dict]:
        columns, statement = QUERIES[name]
        with self.db.cursor() as cur:
            cur.execute(statement, params)
            return [dict(zip(columns, r)) for r in cur.fetchall()]

    def official(self) -> list[tuple]:
        return self.rows("SELECT * FROM official ORDER BY id")

    def binding(self, product: str, key: dict):
        return self.one("SELECT id FROM official WHERE closed_at IS NULL AND product = %s"
                        " AND key = %s::jsonb", (product, json.dumps(key)))

    def bound_job(self, product: str, key: dict):
        return self.one("SELECT job FROM official WHERE closed_at IS NULL AND product = %s"
                        " AND key = %s::jsonb", (product, json.dumps(key)))

    def live(self) -> tuple:
        rows = self.rows("SELECT id, run FROM official WHERE closed_at IS NULL AND run IS NOT NULL"
                         " AND survey = %s AND dataset = %s", (SURVEY, DATASET))
        return rows[0] if rows else (None, None)

    def job(self, job: int) -> dict:
        (row,) = self.rows("SELECT state, refusal, published_attempt, launched, ended"
                           " FROM jobs WHERE id = %s", (job,))
        return dict(zip(("state", "refusal", "published_attempt", "launched", "ended"), row))

    def command(self, *argv) -> list[str]:
        return [sys.executable, "-m", "rapidpipe.cli.main", *map(str, argv)]

    def verb(self, *argv, codes=(0,), pythonpath: Path = REPO) -> tuple[int, str, str]:
        """Run one ``rapidpipe`` verb in its own process; return (exit code, stdout, stderr)."""
        done = subprocess.run(self.command(*argv), env={**self.env, "PYTHONPATH": str(pythonpath)},
                              capture_output=True, text=True, cwd=pythonpath)
        if done.returncode not in codes:
            raise Failed(f"rapidpipe {' '.join(map(str, argv[:3]))} exited {done.returncode}:"
                         f" {done.stderr.strip()[-400:]}")
        return done.returncode, done.stdout, done.stderr

    def write(self, name: str, value) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(value) if not isinstance(value, str) else value)
        return path

    def run_create(self, why: str, settings: dict, revision: str | None = None,
                   pythonpath: Path = REPO) -> int:
        path = self.write(f"settings-{time.monotonic_ns()}.json", settings)
        _, out, _ = self.verb("run", "create", "--who", WHO, "--why", why, "--revision",
                              revision or self.revision, "--settings", path, "--survey", SURVEY,
                              "--dataset", DATASET, pythonpath=pythonpath)
        return int(out.strip())

    def change(self, group: list[dict], why: str, codes=(0,)) -> tuple[int, str, str]:
        return self.verb("official", "change", "--group", self.write(
            f"group-{time.monotonic_ns()}.json", group), "--who", WHO, "--why", why, codes=codes)

    def job_local_argv(self, run: int, stage: str, scope: dict, inputs=(), expects=(),
                       excludes=()) -> list:
        argv = ["job", "local", "--run", run, "--stage", stage, "--scope", json.dumps(scope),
                "--inputs", json.dumps(list(inputs)), "--expects", json.dumps(list(expects))]
        if excludes:
            argv += ["--excludes", json.dumps(list(excludes))]
        return argv

    def job_local(self, run: int, stage: str, scope: dict, inputs=(), expects=(), excludes=(),
                  bound: bool = True) -> dict:
        """``job local``; ``bound`` is whether the automatic binding is expected (exit 0) or refused (1)."""
        _, out, _ = self.verb(*self.job_local_argv(run, stage, scope, inputs, expects, excludes),
                              codes=(0,) if bound else (1,))
        return json.loads(out)

    # -- the launcher: what a loop or a person would choose ------------------

    def import_epoch(self, run: int, epoch: int, ids_offset: int = 0,
                     products=("exposures", "l2files", "diffimages", "sources")) -> int:
        e = EPOCHS[epoch]
        scope = {"source_dir": str(self.args.data_dir),
                 "epochs": [{**e, "rid": e["rid"] + ids_offset, "pid": e["pid"] + ids_offset}]}
        keys = {"exposures": {"dataset": DATASET, "expid": e["expid"]}}
        expects = [{"product": p, "key": keys.get(p, ikey(epoch)),
                    "binding": self.binding(p, keys.get(p, ikey(epoch)))} for p in products]
        inputs = [{"product": "psfs", "key": PSF_KEY, "source_job": self.imports["shared"]}]
        return self.job_local(run, "import", scope, inputs, expects)["job"]

    def associate(self, run: int, source_sets: list[int], predecessor: int | None,
                  excludes: list[int] = (), expects="current", bound: bool = True) -> int:
        inputs = [{"product": "sources", "key": self.source_key(s), "source_job": s}
                  for s in source_sets]
        if predecessor is not None:
            inputs.append({"product": "objects", "key": OKEY, "source_job": predecessor})
        binding = self.binding("objects", OKEY) if expects == "current" else expects
        return self.job_local(
            run, "associate", {"field": FIELD}, inputs,
            [{"product": "objects", "key": OKEY, "binding": binding}],
            [{"key": self.source_key(s), "source_job": s} for s in excludes], bound)["job"]

    def packet_launch(self, epoch: int, object_set: int, window=(1,), predecessors=(),
                      source_set: int | None = None, expects="current") -> tuple[dict, list, list]:
        """The packet job's scope, inputs and expects, chosen now."""
        inputs = [{"product": "objects", "key": OKEY, "source_job": object_set},
                  {"product": "sources", "key": ikey(epoch),
                   "source_job": source_set or self.imports[epoch]},
                  {"product": "psfs", "key": PSF_KEY, "source_job": self.imports["shared"]},
                  {"product": "catalogs", "key": GAIA_KEY, "source_job": self.imports["shared"]},
                  {"product": "refimages", "key": REF_KEY, "source_job": self.imports["shared"]}]
        for w in window:
            inputs += [{"product": p, "key": ikey(w), "source_job": self.imports[w]}
                       for p in ("diffimages", "l2files")]
        inputs += [{"product": "packets", "key": ikey(self.packet_epoch(p)), "source_job": p}
                   for p in predecessors]
        binding = self.binding("packets", ikey(epoch)) if expects == "current" else expects
        scope = {"expid": EPOCHS[epoch]["expid"], "sca": SCA, "fid": FID}
        return scope, inputs, [{"product": "packets", "key": ikey(epoch), "binding": binding}]

    def packet(self, run: int, epoch: int, object_set: int, bound: bool = True, **launch) -> int:
        scope, inputs, expects = self.packet_launch(epoch, object_set, **launch)
        return self.job_local(run, "packet", scope, inputs, expects, bound=bound)["job"]

    def recipe(self, run: int, bound: bool, epochs=(1, 2), predecessor: int | None = None,
               earlier_packets=()) -> dict:
        """Associate and packet over ``epochs`` under ``run``, each set the next one's predecessor."""
        made = {"objects": {}, "packets": {}}
        earlier = list(earlier_packets)
        for epoch in epochs:
            predecessor = self.associate(run, [self.imports[epoch]], predecessor, bound=bound)
            made["objects"][epoch] = predecessor
            made["packets"][epoch] = self.packet(
                run, epoch, predecessor, bound=bound, window=tuple(range(1, epoch + 1)),
                predecessors=list(earlier))
            earlier.append(made["packets"][epoch])
        return made

    def cutover(self, to_run: int, made: dict) -> list[dict]:
        """Rebind the object set and every packet set ``made`` names, and make ``to_run`` live."""
        group = [{"close": self.binding("objects", OKEY),
                  "open": {"product": "objects", "key": OKEY,
                           "job": made["objects"][max(made["objects"])]}}]
        for epoch, job in sorted(made["packets"].items()):
            group.append({"close": self.binding("packets", ikey(epoch)),
                          "open": {"product": "packets", "key": ikey(epoch), "job": job}})
        group.append({"close": self.live()[0],
                      "open": {"survey": SURVEY, "dataset": DATASET, "run": to_run}})
        return group

    def source_key(self, source_set: int) -> dict:
        (key,) = self.rows("SELECT DISTINCT jsonb_build_object('dataset', dataset, 'expid', expid,"
                           " 'sca', sca, 'fid', fid) FROM sources WHERE job = %s", (source_set,))[0]
        return key

    def packet_epoch(self, packet_job: int) -> int:
        expid = self.one("SELECT DISTINCT expid FROM packets WHERE job = %s", (packet_job,))
        return next(e for e, v in EPOCHS.items() if v["expid"] == expid)

    def reader(self) -> tuple[str, set[int]]:
        """``scripts/read_packets.py`` over every ``packets_official`` set: (verdict line, jobs)."""
        done = subprocess.run([sys.executable, str(REPO / "scripts/read_packets.py")],
                              env=self.env, capture_output=True, text=True)
        lines = done.stdout.splitlines()
        jobs = {int(line.split("|")[1]) for line in lines
                if line.startswith("| ") and line.split("|")[1].strip().isdigit()}
        check(done.returncode == 0 and lines[0].endswith("CLEAN"),
              f"reader not clean: {done.stdout.strip()[-600:]}")
        return lines[0], jobs

    def members(self, set_job: int) -> set[tuple]:
        return set(self.rows("SELECT aid, source_job, sid FROM members_official"
                             " WHERE set_job = %s", (set_job,)))

    def chain_mjds(self, set_job: int, aid: int) -> list[float]:
        """The detection times of object ``aid`` through a named set's chain (no set of it excludes any)."""
        return [r[0] for r in self.rows("""
            WITH RECURSIVE chain (job) AS (
                SELECT %s::bigint
                UNION ALL SELECT i.source_job FROM chain c JOIN job_inputs i ON i.job = c.job
                    AND i.kind = 'input' AND i.product = 'objects' WHERE i.source_job IS NOT NULL)
            SELECT s.mjdobs FROM chain c JOIN memberships m ON m.job = c.job
            JOIN sources s ON s.job = m.source_job AND s.sid = m.sid WHERE m.aid = %s""",
            (set_job, aid))]

    def wait_blocked_by(self, holder_pid: int, application: str, timeout: float = 300.0) -> None:
        """Return once a backend named ``application`` waits on ``holder_pid``'s lock."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.one("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s"
                        " AND %s = ANY(pg_blocking_pids(pid))", (application, holder_pid)):
                return
            time.sleep(0.1)
        raise Failed(f"no {application} backend blocked on {holder_pid} within {timeout:.0f} s")

    # -- the steps -------------------------------------------------------------

    def step_1(self) -> str:
        a = self.made["A"] = {"run": self.run_create("recipe A: the thin settings, live",
                                                     self.settings)}
        self.change([{"open": {"survey": SURVEY, "dataset": DATASET, "run": a["run"]}}],
                    "recipe A goes live")
        self.imports["shared"] = self.job_local(
            a["run"], "import", {"source_dir": str(self.args.data_dir), **SHARED}, [],
            [{"product": p, "key": k, "binding": None}
             for p, k in (("refimages", REF_KEY), ("psfs", PSF_KEY), ("catalogs", GAIA_KEY))])["job"]
        self.imports[1] = self.import_epoch(a["run"], 1)
        made = a["made"] = {"objects": {}, "packets": {}}
        made["objects"][1] = self.associate(a["run"], [self.imports[1]], None)
        made["packets"][1] = self.packet(a["run"], 1, made["objects"][1])
        set_1_binding = self.binding("objects", OKEY)
        check(self.bound_job("objects", OKEY) == made["objects"][1], "set 1 not bound after epoch 1")
        self.imports[2] = self.import_epoch(a["run"], 2)
        made["objects"][2] = self.associate(a["run"], [self.imports[2]], made["objects"][1])
        made["packets"][2] = self.packet(a["run"], 2, made["objects"][2], window=(1, 2),
                                         predecessors=[made["packets"][1]])
        set_2 = made["objects"][2]
        check(self.bound_job("objects", OKEY) == set_2, "set 2 not bound after epoch 2")
        closed_by, opened_by = self.rows(
            "SELECT c.closed_by, n.opened_by FROM official c JOIN official n"
            " ON n.opened_group = c.closed_group WHERE c.id = %s AND n.job = %s",
            (set_1_binding, set_2))[0]
        check((closed_by, opened_by) == ("publisher", "publisher"),
              "the move was not the publisher's automatic group")
        expected = self.one("SELECT binding FROM job_inputs WHERE job = %s AND kind = 'expects'",
                            (set_2,))
        check(expected == set_1_binding, "set 2 did not expect set 1's binding")
        own = set(self.rows("SELECT aid, source_job, sid FROM memberships WHERE job = %s", (set_2,)))
        first = set(self.rows("SELECT aid, source_job, sid FROM memberships WHERE job = %s",
                              (made["objects"][1],)))
        check(self.members(set_2) == own | first and own and first,
              "set 2's members are not its own plus set 1's")
        check(all(self.bound_job("packets", ikey(e)) == made["packets"][e] for e in (1, 2)),
              "packet sets not bound")
        return (f"object binding moved set 1 (job {made['objects'][1]}, binding {set_1_binding})"
                f" -> set 2 (job {set_2}) by the publisher; members_official = {len(own)} own"
                f" + {len(first)} of set 1")

    def step_2(self) -> str:
        a = self.made["A"]
        expected = self.binding("objects", OKEY)  # the launcher's choice, made once for both
        candidates = [self.associate(a["run"], [self.imports[2]], a["made"]["objects"][1],
                                     expects=expected, bound=b) for b in (True, False)]
        first, second = candidates
        self.made["candidate"] = first
        check(self.bound_job("objects", OKEY) == first, "the first candidate is not bound")
        shown = self.job(second)
        check(shown["state"] == "published"
              and shown["refusal"] == f"binding {expected} is no longer open",
              f"second candidate: {shown}")
        check(self.one("SELECT count(*) FROM objects WHERE job = %s", (second,)) > 0,
              "second candidate has no rows")
        check(self.one("SELECT count(*) FROM official WHERE job = %s", (second,)) == 0,
              "second candidate holds a binding")
        recorded = set(self.rows("SELECT kind, product, source_job, binding FROM job_inputs"
                                 " WHERE job = %s", (second,)))
        check({("input", "objects", a["made"]["objects"][1], None),
               ("expects", "objects", None, expected)} <= recorded, "inputs not recorded")
        unbound = {r["job"]: r for r in self.query("failed_and_unbound_jobs")}
        check(second in unbound and not unbound[second]["was_bound"],
              "failed_and_unbound_jobs does not show the second candidate")
        return (f"both read set 1 expecting binding {expected}; job {first} bound, job {second}"
                f" published unbound, refusal '{shown['refusal']}', inputs and expects recorded")

    def step_3(self) -> str:
        settings = json.loads(json.dumps(self.settings))
        settings["associate"].update(min_snr=3.0, match_radius=30.0 / 3600)
        self.settings_b = settings
        before = self.official()
        b = self.made["B"] = {"run": self.run_create(
            "recipe B: min_snr 3 and a 30 arcsecond radius, not live", settings)}
        b["made"] = self.recipe(b["run"], bound=False)
        check(self.official() == before, "the official table changed")
        objects = b["made"]["objects"]
        check(self.one("SELECT source_job FROM job_inputs WHERE job = %s AND kind = 'input'"
                       " AND product = 'objects'", (objects[2],)) == objects[1],
              "B's epoch-2 set does not read B's epoch-1 set")
        refusals = {r[0] for r in self.rows("SELECT refusal FROM jobs WHERE run = %s", (b["run"],))}
        check(refusals == {f"run {b['run']} is not live for its survey and dataset"},
              f"B's refusals: {refusals}")
        counts = self.rows("SELECT (SELECT count(*) FROM objects WHERE job = %s),"
                           " (SELECT count(*) FROM objects WHERE job = %s),"
                           " (SELECT count(*) FROM packets WHERE job = ANY(%s))",
                           (objects[2], self.made["candidate"], list(b["made"]["packets"].values())))[0]
        check(all(counts), "B's rows are missing")
        multi = self.one("SELECT count(*) FROM objects WHERE job = %s AND nsources > 1", (objects[2],))
        b["multi"] = multi
        return (f"run {b['run']} epoch 2 set (job {objects[2]}) read its own epoch 1 set"
                f" (job {objects[1]}); {counts[0]} objects ({multi} multi-epoch) and {counts[2]}"
                f" packets coexist with the live set's {counts[1]}; official unchanged")

    def step_4_and_5(self) -> tuple[str, str]:
        a, b = self.made["A"], self.made["B"]
        # Step 5's late job: launched under recipe A before the cutover, publishing after it.
        scope, inputs, expects = self.packet_launch(2, a["made"]["objects"][2], window=(1, 2),
                                                    predecessors=[a["made"]["packets"][1]])
        late = subprocess.Popen(self.command(*self.job_local_argv(a["run"], "packet", scope,
                                                                  inputs, expects)),
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        deadline = time.monotonic() + 120
        while not self.one("SELECT count(*) FROM jobs WHERE run = %s AND stage = 'packet'"
                           " AND state = 'running' AND id > %s", (a["run"], b["made"]["packets"][2])):
            check(time.monotonic() < deadline and late.poll() is None, "the late job never ran")
            time.sleep(0.05)
        _, out, _ = self.change(self.cutover(b["run"], b["made"]), "cutover to recipe B")
        group = self.cutover_b = int(out.strip())
        out, err = late.communicate(timeout=600)
        result = json.loads(out)
        late_job = result["job"]

        opened = self.rows("SELECT product, job, run FROM official WHERE opened_group = %s", (group,))
        closed = self.rows("SELECT id FROM official WHERE closed_group = %s", (group,))
        check(len(opened) == len(closed) == 4, f"group opened {len(opened)}, closed {len(closed)}")
        check(self.live()[1] == b["run"], "B is not live")
        verdict, seen = self.reader()
        check(seen == set(b["made"]["packets"].values()), f"reader saw {seen}")
        lineage = {}
        for job in seen:
            ancestors = {r["job"] for r in self.query("ancestors_of_packet_set", packet_job=job)}
            object_jobs = {r[0] for r in self.rows(
                "SELECT DISTINCT object_job FROM packets_official WHERE job = %s", (job,))}
            check(object_jobs <= set(b["made"]["objects"].values()) and object_jobs <= ancestors,
                  f"packet set {job}'s index names {object_jobs}")
            check({self.imports["shared"], b["made"]["objects"][1]} <= ancestors,
                  f"packet set {job}'s ancestors {ancestors}")
            lineage[job] = sorted(ancestors)
        step_4 = (f"group {group} opened and closed 4 (objects, 2 packet sets, live); reader"
                  f" '{verdict}' on B's sets {sorted(seen)}; ancestors {lineage}")

        cutover_at = self.one("SELECT opened_at FROM official WHERE opened_group = %s LIMIT 1",
                              (group,))
        shown = self.job(late_job)
        check(late.returncode == 1 and result["published"] and result["group"] is None,
              f"late job: exit {late.returncode} {result} {err[-300:]}")
        check(shown["launched"] < cutover_at < shown["ended"],
              "the late job was not launched before the cutover and published after it")
        check(shown["refusal"] == f"run {a['run']} is not live for its survey and dataset",
              f"late job refusal {shown['refusal']}")
        check(self.one("SELECT count(*) FROM packets WHERE job = %s", (late_job,)) > 0
              and self.one("SELECT count(*) FROM official WHERE job = %s", (late_job,)) == 0,
              "the late job's rows are missing or bound")
        step_5 = (f"job {late_job} (recipe A) launched before group {group}, published after it:"
                  f" {self.one('SELECT count(*) FROM packets WHERE job = %s', (late_job,))} rows,"
                  f" unbound, refusal '{shown['refusal']}'")
        return step_4, step_5

    def step_6(self) -> str:
        a, b = self.made["A"], self.made["B"]
        replaced = {r[0] for r in self.rows("SELECT id FROM official WHERE closed_group = %s",
                                            (self.cutover_b,))}
        _, out, _ = self.verb("official", "undo", self.cutover_b, "--who", WHO,
                              "--why", "undo the cutover to recipe B")
        check(self.live()[1] == a["run"], "A is not live again")
        verdict, seen = self.reader()
        check(seen == set(a["made"]["packets"].values()), f"reader saw {seen}")
        check(self.bound_job("objects", OKEY) == self.made["candidate"], "objects not restored")
        open_ids = {r[0] for r in self.rows("SELECT id FROM official WHERE closed_at IS NULL")}
        check(not replaced & open_ids, "a closed row was reopened")
        kept = self.one("SELECT count(*) FROM packets WHERE job = ANY(%s)",
                        (list(b["made"]["packets"].values()),))
        check(kept > 0, "B's packets are gone")
        return (f"undo group {out.strip()}: reader '{verdict}' on A's sets {sorted(seen)}; A live;"
                f" restored bindings are new rows; B's {kept} packet rows still exist")

    def step_7(self) -> str:
        b = self.made["B"]
        object_binding = self.binding("objects", OKEY)
        epoch_2 = self.binding("packets", ikey(2))
        c = self.made["C"] = {"run": self.run_create(
            "recipe C: epoch 1 packets again from recipe B's object set", self.settings_b)}
        b2 = b["made"]["objects"][2]
        job = c["packets"] = self.packet(c["run"], 1, b2, bound=False, window=(1,))
        self.change([{"close": self.binding("packets", ikey(1)),
                      "open": {"product": "packets", "key": ikey(1), "job": job}}],
                    "bind recipe C's epoch 1 packets")
        check(self.binding("objects", OKEY) == object_binding, "the object binding moved")
        check(self.binding("packets", ikey(2)) == epoch_2, "epoch 2's packets moved")
        check(self.bound_job("packets", ikey(1)) == job, "C's packets not bound")
        history = self.query("packet_history_by_observation", dataset=DATASET,
                             expid=EPOCHS[1]["expid"], sca=SCA, fid=FID)
        check({r["object_job"] for r in history if r["job"] == job} == {b2},
              "the index does not name B's object set")
        epoch_2_mjd = self.one("SELECT DISTINCT mjdobs FROM sources WHERE job = %s",
                               (self.imports[2],))
        later = 0
        (filename,) = self.rows("SELECT DISTINCT filename FROM packets WHERE job = %s", (job,))[0]
        with local_path(filename).open("rb") as stream:
            records = list(fastavro.reader(stream))
        for record in records:
            trigger = record["diaSource"]["midpointMjd"]
            check(all(p["midpointMjd"] < trigger for p in record["prvDiaSources"] or []),
                  "a previous detection does not precede its trigger")
            check(all(p["midpointMjd"] != epoch_2_mjd for p in record["prvDiaSources"] or []),
                  "an epoch 2 detection is in an epoch 1 packet")
            aid = record["diaObject"]["diaObjectId"]
            members = self.chain_mjds(b2, aid)
            check(record["diaObject"]["nDiaSources"] == sum(1 for m in members if m <= trigger),
                  f"object {aid}: nDiaSources {record['diaObject']['nDiaSources']}, members {members}")
            later += any(m == epoch_2_mjd for m in members)
        check(later > 0, "no object of B's set has an epoch 2 member: the bound is vacuous")
        return (f"run {c['run']} job {job} from B's set (job {b2}) bound for epoch 1 only;"
                f" object and epoch 2 bindings untouched; {len(records)} packets, {later} of"
                f" whose objects have an epoch 2 member, carry none; index names job {b2}")

    def step_8(self) -> str:
        a = self.made["A"]
        publisher = sys.modules["rapidpipe.foundation.publish"]
        runner = sys.modules["rapidpipe.foundation.local"]
        loaded = []

        class Killed(BaseException):
            """The execution was killed."""

        def killed_before_commit(cur, job_id, run_id):
            cur.execute("SELECT count(*) FROM packets WHERE job = %s", (job_id,))
            loaded.append(cur.fetchone()[0])
            raise Killed

        real_publish = runner.publish

        def killed_after_commit(*args, **kwargs):
            real_publish(*args, **kwargs)
            raise Killed

        scope, inputs, expects = self.packet_launch(2, a["made"]["objects"][2], window=(1, 2),
                                                    predecessors=[a["made"]["packets"][1]])
        argv = self.job_local_argv(a["run"], "packet", scope, inputs, expects)
        with patched(publisher, "_bind", killed_before_commit), contextlib.suppress(Killed), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(list(map(str, argv)))
        job = self.one("SELECT max(id) FROM jobs WHERE stage = 'packet'")

        def counts():
            return self.rows("SELECT (SELECT count(*) FROM packets WHERE job = %(j)s),"
                             " (SELECT count(*) FROM forced WHERE job = %(j)s)", {"j": job})[0]

        check(loaded and loaded[0] > 0, "the kill came before the rows were loaded")
        check(counts() == (0, 0) and self.job(job)["state"] == "failed",
              f"after the first kill: {counts()} {self.job(job)}")
        with patched(runner, "publish", killed_after_commit), contextlib.suppress(Killed), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["job", "run", "--job", str(job)])
        published = counts()
        check(published[0] == loaded[0] and self.job(job)["published_attempt"] == 2,
              f"the retry: {published} {self.job(job)}")
        _, out, _ = self.verb("job", "run", "--job", job)
        again = json.loads(out)
        attempts = sorted(p.name for p in (local_path(self.args.outputs_root) / str(a["run"])
                                           / "packet" / str(job)).iterdir())
        check(not again["published"] and counts() == published and attempts == ["1", "2"],
              f"the second retry: {again} {counts()} {attempts}")
        check(self.bound_job("packets", ikey(2)) == job, "the retry did not bind")
        return (f"job {job}: killed after loading {loaded[0]} rows, before commit -> 0 rows,"
                f" failed; job run published attempt 2 once ({published[0]} + {published[1]} rows)"
                f" and was killed after commit; job run again: no-op, attempts {attempts}")

    def step_9(self) -> str:
        scratch = self.args.scratch
        trees = [scratch / "person-p", scratch / "person-q"]
        for tree in trees:
            self.git("worktree", "add", "--detach", str(tree), self.revision)
        toml = trees[1] / "rapidpipe/settings/foundation-thin.toml"
        toml.write_text(toml.read_text().replace("min_snr = 5.0", "min_snr = 3.0"))
        self.git("-c", f"user.name={WHO}", "-c", "user.email=thin-proof@localhost", "commit", "-q",
                 "-am", "proof step 9: person Q lowers min_snr to 3", cwd=trees[1])
        try:
            runs, procs = [], []
            for name, tree in zip("PQ", trees):
                revision = self.git("rev-parse", "HEAD", cwd=tree)
                settings = tomllib.loads(
                    (tree / "rapidpipe/settings/foundation-thin.toml").read_text())
                run = self.run_create(f"person {name}'s recipe from {revision[:8]}", settings,
                                      revision, pythonpath=tree)
                runs.append((run, revision))
            for (run, _), tree in zip(runs, trees):
                argv = self.job_local_argv(run, "associate", {"field": FIELD}, [
                    {"product": "sources", "key": ikey(1), "source_job": self.imports[1]}],
                    [{"product": "objects", "key": OKEY, "binding": self.binding("objects", OKEY)}])
                procs.append(subprocess.Popen(self.command(*argv),
                                              env={**self.env, "PYTHONPATH": str(tree)},
                                              cwd=tree, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True))
            jobs = []
            for proc in procs:
                out, err = proc.communicate(timeout=600)
                check(proc.returncode == 1, f"person's job exited {proc.returncode}: {err[-300:]}")
                result = json.loads(out)
                check(result["published"], f"not published: {result}")
                jobs.append(result["job"])
        finally:
            for tree in trees:
                self.git("worktree", "remove", "--force", str(tree))
        (p_run, p_rev), (q_run, q_rev) = runs
        check(p_rev != q_rev and self.rows("SELECT revision FROM runs WHERE id = ANY(%s)"
                                           " ORDER BY id", ([p_run, q_run],)) == [(p_rev,), (q_rev,)],
              "the runs do not record their two commits")
        overlap = self.one("SELECT max(started) < min(ended) FROM jobs WHERE id = ANY(%s)", (jobs,))
        per_source = self.rows("""
            WITH assigned AS (
                SELECT m.job, s.dataset, s.expid, s.sca, s.fid, s.sid, m.aid
                FROM memberships m JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
                WHERE m.job = ANY(%(jobs)s))
            SELECT count(*) FILTER (WHERE p.aid IS NOT NULL AND q.aid IS NOT NULL),
                   count(*) FILTER (WHERE p.aid IS NULL),
                   count(*) FILTER (WHERE q.aid IS NULL)
            FROM (SELECT * FROM assigned WHERE job = %(p)s) p
            FULL JOIN (SELECT * FROM assigned WHERE job = %(q)s) q
              ON (q.dataset, q.expid, q.sca, q.fid, q.sid) = (p.dataset, p.expid, p.sca, p.fid, p.sid)
            """, {"jobs": jobs, "p": jobs[0], "q": jobs[1]})[0]
        coincide, p_objects, q_objects = self.rows(
            "SELECT (SELECT count(*) FROM objects p JOIN objects q ON q.aid = p.aid"
            "        WHERE p.job = %(p)s AND q.job = %(q)s),"
            " (SELECT count(*) FROM objects WHERE job = %(p)s),"
            " (SELECT count(*) FROM objects WHERE job = %(q)s)", {"p": jobs[0], "q": jobs[1]})[0]
        check(per_source[0] > 0 and per_source[1] > 0 and coincide > 0,
              f"the joins: per source {per_source}, coincide {coincide}")
        return (f"runs {p_run} ({p_rev[:8]}) and {q_run} ({q_rev[:8]}), jobs {jobs} concurrent"
                f" ({'overlapping' if overlap else 'NOT overlapping'}), both published; per source"
                f" on the image key and sid: {per_source[0]} in both, {per_source[1]} only Q's;"
                f" on aid: {coincide} of P's {p_objects} and Q's {q_objects} objects coincide")

    def step_10(self) -> str:
        contested = self.binding("objects", OKEY)
        bystander = self.binding("diffimages", ikey(1))
        check(bystander < contested, "the bystander binding is not the older one")
        holder = psycopg2.connect(dbname=self.args.database, application_name="thin-proof-a")
        with holder.cursor() as cur:
            apply(cur, [{"close": contested, "open": {"product": "objects", "key": OKEY,
                                                      "job": self.made["candidate"]}}],
                  "session-a", "proof step 10: session A rebinds the object set")
        group = self.write("group-b.json", [
            {"close": bystander},
            {"close": contested, "open": {"product": "objects", "key": OKEY,
                                          "job": self.made["B"]["made"]["objects"][2]}}])
        b = subprocess.Popen(self.command("official", "change", "--group", group, "--who",
                                          "session-b", "--why", "proof step 10: session B"),
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_blocked_by(holder.get_backend_pid(), "rapidpipe-official-change")
        holder.commit()
        holder.close()
        _, err = b.communicate(timeout=60)
        check(b.returncode == 1 and f"binding {contested} is no longer open" in err,
              f"session B: exit {b.returncode} {err.strip()}")
        check(self.binding("diffimages", ikey(1)) == bystander, "the bystander moved")
        check(self.one("SELECT count(*) FROM official WHERE opened_by = 'session-b'"
                       " OR closed_by = 'session-b'") == 0, "session B left a row")
        check(self.bound_job("objects", OKEY) == self.made["candidate"]
              and self.binding("objects", OKEY) != contested, "session A's group did not apply")
        return (f"A held its group on binding {contested}; B (official change) blocked, then"
                f" refused naming it, exit 1; bystander {bystander} still open; no row of B's")

    def step_11(self) -> str:
        a, b = self.made["A"], self.made["B"]
        expected = self.binding("packets", ikey(2))
        cutover = self.cutover(b["run"], b["made"])
        holder = psycopg2.connect(dbname=self.args.database, application_name="thin-proof-a")
        with holder.cursor() as cur:
            group = apply(cur, cutover, "session-a", "proof step 11: cutover to recipe B")
        scope, inputs, expects = self.packet_launch(2, a["made"]["objects"][2], window=(1, 2),
                                                    predecessors=[a["made"]["packets"][1]])
        racer = subprocess.Popen(self.command(*self.job_local_argv(a["run"], "packet", scope,
                                                                   inputs, expects)),
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        self.wait_blocked_by(holder.get_backend_pid(), "rapidpipe-job-local")
        holder.commit()
        holder.close()
        out, err = racer.communicate(timeout=600)
        result = json.loads(out)
        check(racer.returncode == 1 and result["published"] and result["group"] is None
              and result["refusal"] == f"binding {expected} is no longer open",
              f"racer: exit {racer.returncode} {result} {err[-300:]}")
        rows = self.one("SELECT count(*) FROM packets WHERE job = %s", (result["job"],))
        check(rows > 0 and self.one("SELECT count(*) FROM official WHERE job = %s",
                                    (result["job"],)) == 0, "the racer's rows are missing or bound")
        packet_bindings = self.rows("SELECT key, job, opened_group FROM official"
                                    " WHERE closed_at IS NULL AND product = 'packets'")
        check(sorted(k["expid"] for k, _, _ in packet_bindings)
              == sorted(EPOCHS[e]["expid"] for e in (1, 2))
              and {(j, g) for _, j, g in packet_bindings}
              == {(j, group) for j in b["made"]["packets"].values()},
              f"packet bindings {packet_bindings}")
        check(self.live()[1] == b["run"], "B is not live")
        return (f"A held cutover group {group}; job {result['job']} (recipe A, live) blocked in"
                f" publish, then {rows} rows published unbound, refusal '{result['refusal']}';"
                f" one selection: both packet keys B's from group {group}, B live")

    def step_12(self) -> str:
        b = self.made["B"]
        d = self.made["D"] = {"run": self.run_create("recipe D: cut over at epoch 1",
                                                     self.settings)}
        d["made"] = self.recipe(d["run"], bound=False, epochs=(1,))
        before_epoch_1 = self.bound_job("packets", ikey(1))
        _, out, _ = self.change(self.cutover(d["run"], d["made"]), "cutover to recipe D at epoch 1")
        cutover = int(out.strip())
        moved = self.binding("objects", OKEY)
        progress = self.recipe(d["run"], bound=True, epochs=(2,), predecessor=d["made"]["objects"][1],
                               earlier_packets=[d["made"]["packets"][1]])
        check(self.bound_job("objects", OKEY) == progress["objects"][2], "epoch 2 did not bind")
        before = self.official()
        _, _, err = self.verb("official", "undo", cutover, "--who", WHO, "--why",
                                 "undo the cutover to recipe D", codes=(1,))
        check(f"binding {moved}" in err and self.official() == before,
              f"undo: {err.strip()}")
        self.change([{"close": self.binding("packets", ikey(1)),
                      "open": {"product": "packets", "key": ikey(1), "job": before_epoch_1}},
                     {"close": self.live()[0],
                      "open": {"survey": SURVEY, "dataset": DATASET, "run": b["run"]}}],
                    "restore the keys still as recipe D's cutover left them")
        check(self.bound_job("packets", ikey(1)) == before_epoch_1
              and self.bound_job("packets", ikey(2)) == progress["packets"][2]
              and self.bound_job("objects", OKEY) == progress["objects"][2]
              and self.live()[1] == b["run"], "the restoring group's result")
        return (f"cutover group {cutover}, then D's epoch 2 (job {progress['objects'][2]}) bound;"
                f" undo refused naming binding {moved}, official unchanged; explicit group"
                f" restored epoch 1's packets (job {before_epoch_1}) and B live; epoch 2 stays D's")

    def step_13(self) -> str:
        e = self.made["E"] = {"run": self.run_create("recipe E: a superseded source set",
                                                     self.settings)}
        self.change([{"close": self.live()[0],
                      "open": {"survey": SURVEY, "dataset": DATASET, "run": e["run"]}}],
                    "recipe E goes live")
        superseded = self.imports[1]
        set_1 = self.associate(e["run"], [superseded], None)
        replacement = self.import_epoch(e["run"], 1, REIMPORT_ID_OFFSET,
                                        products=("diffimages", "sources"))
        check(self.bound_job("sources", ikey(1)) == replacement
              and self.bound_job("diffimages", ikey(1)) == replacement,
              "the replacement is not bound")
        set_2 = self.associate(e["run"], [self.imports[2], replacement], set_1,
                               excludes=[superseded])
        check(self.bound_job("objects", OKEY) == set_2, "set 2 is not bound")
        check(superseded not in {r[0] for r in self.rows(
            "SELECT DISTINCT source_job FROM members_official WHERE set_job = %s", (set_2,))},
              "members_official returns a member of the superseded set")
        sums = self.rows("""
            SELECT o.aid, o.nsources, o.sumflux, count(s.sid), coalesce(sum(s.fluxfit), 0)
            FROM objects_official o
            LEFT JOIN members_official m ON m.set_job = o.job AND m.aid = o.aid
            LEFT JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
            GROUP BY o.aid, o.nsources, o.sumflux""")
        for aid, nsources, sumflux, n, flux in sums:
            check(nsources == n and abs(sumflux - flux) <= 1e-9 * max(1.0, abs(flux)),
                  f"object {aid}: sums ({nsources}, {sumflux}) against members ({n}, {flux})")
        dropped = self.one("SELECT (manifest -> 'associate' ->> 'excluded_members')::int FROM jobs"
                           " WHERE id = %s", (set_2,))
        old = self.rows("SELECT (SELECT count(*) FROM sources WHERE job = %(s)s),"
                        " (SELECT count(*) FROM memberships WHERE job = %(j)s"
                        "  AND source_job = %(s)s)", {"s": superseded, "j": set_1})[0]
        check(dropped and dropped == old[1] and old[0] > 0, f"dropped {dropped}, old rows {old}")
        return (f"replacement import job {replacement} rebound epoch 1; set 2 (job {set_2}) told"
                f" to exclude job {superseded} dropped {dropped} members; {len(sums)} objects'"
                f" sums equal their members; old {old[0]} sources and {old[1]} memberships remain")


@contextlib.contextmanager
def patched(module, name, value):
    """The monkeypatch the proof owns: ``module.name`` is ``value`` inside the block."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def fresh_database(name: str) -> None:
    admin = psycopg2.connect(dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cur.fetchone():
            raise SystemExit(f"database {name} exists; the proof starts from a fresh one")
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", name, "-f",
                    str(REPO / "database/schema.sql")], check=True, capture_output=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="the field's stored products (FIELD-MANIFEST layout)")
    parser.add_argument("--outputs-root", required=True, help="a file:// URL")
    parser.add_argument("--database", required=True, help="created here; must not exist")
    parser.add_argument("--scratch", type=Path, required=True, help="for step 9's worktrees")
    args = parser.parse_args(argv)
    if not args.outputs_root.endswith("/"):
        args.outputs_root += "/"
    started = time.monotonic()
    fresh_database(args.database)
    proof = Proof(args)
    steps = [("1", proof.step_1), ("2", proof.step_2), ("3", proof.step_3),
             (("4", "5"), proof.step_4_and_5), ("6", proof.step_6), ("7", proof.step_7),
             ("8", proof.step_8), ("9", proof.step_9), ("10", proof.step_10),
             ("11", proof.step_11), ("12", proof.step_12), ("13", proof.step_13)]
    passed = 0
    for names, step in steps:
        names = (names,) if isinstance(names, str) else names
        t0 = time.monotonic()
        try:
            observations = step()
            observations = (observations,) if isinstance(observations, str) else observations
            for name, observation in zip(names, observations):
                print(f"STEP {name} PASS {observation} [{time.monotonic() - t0:.1f} s]", flush=True)
                passed += 1
        except Exception as error:  # noqa: BLE001 - a failed step is reported, the proof stops
            for name in names:
                print(f"STEP {name} FAIL {type(error).__name__}: {error}", flush=True)
            break
    print(f"{passed} of 13 steps passed in {time.monotonic() - started:.1f} s"
          f" (database {args.database})", flush=True)
    return 0 if passed == 13 else 1


if __name__ == "__main__":
    sys.exit(main())
