"""Configuration loading for the RimTimSim detection downselect.

The whole analysis is driven by `rimtimsim.toml`.  Nothing else in the package
hard-codes a path, a job id, or a matrix entry, so re-targeting the analysis at a
different pipeline run means editing one file.
"""
import os
import tomllib


DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rimtimsim.toml")


class Config:
    """Parsed configuration, with the derived paths a stage actually needs."""

    def __init__(self, path=None):
        self.path = path or DEFAULT_CONFIG
        with open(self.path, "rb") as fh:
            self.raw = tomllib.load(fh)

        self.run = self.raw["run"]
        self.survey = self.raw["survey"]
        self.catalogs = self.raw["catalogs"]
        self.truth = self.raw["truth"]
        self.sweep = self.raw["sweep"]
        self.paths = self.raw["paths"]

        lo, hi = self.run["science_jids"]
        self.science_jids = list(range(int(lo), int(hi) + 1))
        self.reference_jids = [int(j) for j in self.run["reference_jids"]]
        self.filters = list(self.survey["filters"])
        self.alias = dict(self.survey["filter_alias"])

    # -- derived paths -----------------------------------------------------

    @property
    def work(self):
        """Working directory for everything this analysis derives.

        `RTS_WORK` overrides the configured path, so the identical config file can
        be used on the host, inside the container where the same directory is
        bind-mounted somewhere else, and by a second person working at their own
        path without editing the file.
        """
        return os.path.abspath(os.environ.get("RTS_WORK") or self.paths["work"])

    @property
    def cache(self):
        """Directory holding pipeline products fetched from S3.

        Deliberately separable from `work`.  The products are large -- a full
        matrix caches ~100 GB of difference images -- and they are read-only
        inputs, so several people can point `RTS_CACHE` (or an absolute
        `[paths] cache`) at ONE shared copy rather than each downloading their
        own.  A relative configured path stays inside `work`, which is what a
        self-contained single-user run wants.
        """
        env = os.environ.get("RTS_CACHE")
        if env:
            return os.path.abspath(env)
        c = self.paths["cache"]
        return c if os.path.isabs(c) else os.path.join(self.work, c)

    @property
    def catalog_dir(self):
        """Directory holding the variable delivery archive and its extractions.

        The delivery is 1.17 GB and its extracted members several GB more, and
        like the pipeline products it is a read-only input -- so it is a path,
        not a copy.  `RTS_CATALOGS` or an absolute `[catalogs] dir` points at a
        shared delivery; a relative value stays inside `work`.
        """
        env = os.environ.get("RTS_CATALOGS")
        if env:
            return os.path.abspath(env)
        d = self.catalogs.get("dir", "catalogs")
        return d if os.path.isabs(d) else os.path.join(self.work, d)

    @property
    def keep_images(self):
        """Whether a fetched difference image survives the unit that used it.

        `keep` (the default) makes re-runs free at the cost of ~100 GB.
        `discard` deletes each image as soon as its sweep result is written,
        which bounds a laptop run at a few GB and re-downloads on a re-run.
        `RTS_CACHE_POLICY` overrides the configured value.
        """
        pol = os.environ.get("RTS_CACHE_POLICY") or self.paths.get("cache_policy", "keep")
        if pol not in ("keep", "discard"):
            raise ValueError("cache_policy must be 'keep' or 'discard', not %r" % pol)
        return pol == "keep"

    def wpath(self, *parts):
        """Path inside the working directory, created on demand."""
        p = os.path.join(self.work, *parts)
        os.makedirs(os.path.dirname(p) if os.path.splitext(p)[1] else p, exist_ok=True)
        return p

    def product_uri(self, jid, name):
        return "s3://%s/%s/jid%d/%s" % (
            self.run["product_bucket"], self.run["proc_date"], jid, name)

    def log_uri(self, jid):
        return "s3://%s/%s/rapid_pipeline_job_%s_jid%d_log.txt" % (
            self.run["log_bucket"], self.run["proc_date"], self.run["proc_date"], jid)

    def trexs_filter(self, filt):
        """Roman filter name -> the TRExS spelling used in the catalogue columns."""
        return self.alias[filt]

    def lightcurve_file(self, filt):
        return self.catalogs["lightcurves"].format(trexs_filter=self.trexs_filter(filt))


def load(path=None):
    return Config(path)
