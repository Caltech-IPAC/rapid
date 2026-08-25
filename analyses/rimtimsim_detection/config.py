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
        """Working directory.

        `RTS_WORK` overrides the configured path so the identical config file can
        be used on the host and inside the container, where the same directory is
        bind-mounted somewhere else.
        """
        return os.environ.get("RTS_WORK") or self.paths["work"]

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
