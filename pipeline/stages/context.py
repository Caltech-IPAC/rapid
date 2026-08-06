"""
File:    context.py

`StageContext`: what a stage is handed instead of reaching for a global.

The monoliths shared roughly forty module-level names between their stages —
`s3_client`, `jid`, `job_proc_date`, `product_s3_bucket`, `rapid_sw`,
`upload_to_s3_bucket`, the fourteen live `ConfigParser` section dicts, and the
running `product_config` every stage mutated in place. Extraction has to put
those somewhere, and the choice of where is not cosmetic:

**One object, passed explicitly.** A stage's signature says what it reads. That
is the property the monolith lacked — there, any of the 2,961 lines could read
or rebind any name, which is why `science_image_filename` silently becoming the
fake-source-injected variant at line 886 was invisible to every stage after it.

**Products flow through `products`, not through rebinding.** A stage that
produces a file records it under a name; a later stage asks for it by that
name and gets a `KeyError` — naming the stage that should have produced it — if
it is absent. In the monolith the same mistake produced a `NameError` deep
inside a tool invocation, or worse, silently used a stale value from an earlier
branch. The `rfid is not None` branch is exactly that hazard: it leaves a dozen
names undefined that the other branch defines, and the monolith coped with an
`if rfid is None:` guard 200 lines further down.

**Configuration is read-only and comes from three homes**, matching the
placement criterion (co-design, Principle 4): `science` is release content
(`cdf/science/pipeline.toml`, carried by the image), `parameters` is the
operational parameter tree (SSM, mutable between releases), and `facts` is the
per-invocation manifest. A stage that wants a value knows which kind of value
it is by which accessor it calls, and none of the three can be written to from
a stage — the monolith's habit of mutating `sextractor_diffimage_dict` in place
and reverting two keys afterwards is not expressible here.
"""

import dataclasses
import datetime
from typing import Any

from pipeline.runtime import science_config
from pipeline.runtime.errors import ConfigError, InputError


@dataclasses.dataclass
class StageContext:
    """Everything a stage may read, and the one place it records what it made.

    Constructed once by the entrypoint after startup and handed to every stage
    in the sequence. Not frozen — `products` and `provenance` accumulate as the
    sequence runs — but the configuration mappings it carries are treated as
    read-only by every stage, and the accessors below are the only sanctioned
    way in.

    Attributes
    ----------
    workdir : WorkingDirectory
        The per-attempt tree. All intermediate files live under
        `workdir.scratch(...)`; anything a human would want after a failure
        goes under `workdir.bundle_path(...)`.
    unit : ProcessingUnit
        This array child's processing unit, from the manifest.
    job_type : str
        The manifest's job type — the dispatch discriminator, already
        route-validated by the entrypoint before any stage runs.
    science : dict
        Release-content science configuration (`cdf/science/pipeline.toml`).
    parameters : dict
        The operational parameter tree, relative-keyed (`batch/queue-prompt`).
    logger : RuntimeLogger
        Job- and attempt-tagged. Stages log through this; `print` is banned in
        the operational layer and lint-enforced.
    s3 : object
        A boto3 S3 client, injected so a stage never constructs one.
    products : dict
        Stage outputs by name — the explicit replacement for rebinding shared
        globals. Written through `produce`, read through `product`.
    provenance : dict
        Facts the terminal record should carry: checksums, infobits, counts.
    """

    workdir: Any
    unit: Any
    job_type: str
    science: dict
    parameters: dict
    logger: Any
    s3: Any = None
    products: dict = dataclasses.field(default_factory=dict)
    provenance: dict = dataclasses.field(default_factory=dict)
    started_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    # -- per-invocation facts ------------------------------------------------

    @property
    def facts(self) -> Any:
        """The manifest's `UnitFacts` for this processing unit."""
        return self.unit.facts

    def fact(self, name: str) -> Any:
        """One per-invocation fact, required.

        Raises
        ------
        InputError
            If the manifest did not carry it. `input_missing` rather than
            `config_invalid`: the manifest is this invocation's input, and a
            missing fact means the submitter did not describe the unit fully —
            not that the deployment is misconfigured.
        """
        value = getattr(self.unit.facts, name, None)
        if value is None:
            raise InputError(
                f"the manifest carries no {name!r} for unit "
                f"{self.unit.key}; the job type {self.job_type!r} needs it",
                unit=self.unit.key, fact=name)
        return value

    def optional_fact(self, name: str, default: Any = None) -> Any:
        """One per-invocation fact that is legitimately absent sometimes.

        Used where absence is a real distinction rather than an omission — the
        clearest case being `reference_image_id`, whose absence is what selects
        the build-a-reference-image branch over the use-an-existing-one branch.
        """
        value = getattr(self.unit.facts, name, None)
        return default if value is None else value

    # -- release content -----------------------------------------------------

    def science_section(self, name: str) -> dict:
        """One section of the release's science configuration.

        These are the sections that were `.ini` blocks passed around as live
        `ConfigParser` dicts. A copy is returned, so a stage that mutates what
        it gets — as the six SExtractor blocks did, with a manual save/revert
        protocol between them — cannot affect the next stage.
        """
        return science_config.section(self.science, name)

    def science_value(self, section_name: str, key: str) -> Any:
        """One release-content value, typed as the TOML declares it."""
        return science_config.value(self.science, section_name, key)

    # -- operational configuration -------------------------------------------

    def parameter(self, name: str) -> str:
        """One operational parameter, required.

        Raises
        ------
        ConfigError
            If absent. No default parameter, by the same reasoning as the
            environment contract: a default converts a misconfigured
            deployment into a job that runs against the wrong thing and
            reports success.
        """
        if name not in self.parameters:
            raise ConfigError(
                f"parameter {name!r} is not in the pipeline parameter tree "
                f"(read {len(self.parameters)} parameters); it is operational "
                f"configuration and has no default",
                parameter=name)
        return self.parameters[name]

    # -- products ------------------------------------------------------------

    def produce(self, name: str, value: Any) -> Any:
        """Record a stage output under a name, and return it.

        Returns its argument so a stage can produce and use in one expression.
        Rebinding an existing name is allowed and is sometimes correct — the
        fake-source injection stage legitimately replaces the science image
        every later stage reads — but it is logged, because in the monolith
        that same rebinding was the single hardest thing to see.
        """
        if name in self.products and self.products[name] != value:
            self.logger.info("product %r replaced: %s -> %s", name,
                             self.products[name], value)
        self.products[name] = value
        return value

    def product(self, name: str) -> Any:
        """One product of an earlier stage, required.

        Raises
        ------
        InputError
            If no stage produced it. The message names what is missing rather
            than failing later inside a tool invocation with a `None` argument,
            which is how the monolith failed when a branch skipped a stage.
        """
        if name not in self.products:
            produced = ", ".join(sorted(self.products)) or "nothing yet"
            raise InputError(
                f"no stage has produced {name!r}; this sequence has produced: "
                f"{produced}", product=name)
        return self.products[name]

    def has_product(self, name: str) -> bool:
        """Whether an earlier stage produced this, without requiring it."""
        return name in self.products

    def record(self, **facts: Any) -> None:
        """Add facts to the attempt's provenance.

        Checksums, infobits, source counts: what the terminal record should
        carry about what this attempt produced. Kept separate from `products`
        because products are filenames the next stage consumes, and these are
        values the record consumes.
        """
        self.provenance.update(facts)

    # -- derived paths -------------------------------------------------------

    def scratch(self, *parts: str) -> str:
        """A path for an intermediate file. Dies with the container."""
        return self.workdir.scratch(*parts)

    def bundle(self, *parts: str) -> str:
        """A path for evidence. Uploaded with the diagnostics bundle."""
        return self.workdir.bundle_path(*parts)
