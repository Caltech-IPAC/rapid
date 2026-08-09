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
        globals. Written through `produce`, read through `product`. This is the
        stage-to-stage channel and holds downloaded INPUTS and intermediates as
        well as final outputs; it is not the published-product list.
    published_products : dict
        The products actually uploaded, by name, each with the immutable S3 URI
        and the checksum of the bytes uploaded (review finding #18). Written
        through `publish` after the upload succeeds, and serialized into the
        terminal record as what a registrar registers.
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
    published_products: dict = dataclasses.field(default_factory=dict)
    provenance: dict = dataclasses.field(default_factory=dict)
    started_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    #: This attempt's identity, for product keys (review finding #18). Set by
    #: the entrypoint from the resolved ownership; absent in unit tests that
    #: construct a bare context, where `product_prefix` falls back and says so.
    run_id: Any = None
    attempt_id: Any = None
    #: THE ATTEMPT'S OWN DATABASE CONNECTION, LENT TO STAGES THAT NEED ONE
    #: (post-DB chain conversion). The entrypoint already opens exactly one
    #: connection per attempt, on the route matrix's lane for this job type,
    #: and holds it for the attempt's lifetime. The post-DB job types produce
    #: database state rather than S3 products, so they need that connection —
    #: and the co-design's ruling 4 says which one they get: "the non-identity
    #: per-call database sites inside the converted scripts move to borrowed
    #: connections with the job-type work".
    #:
    #: Borrowed, never opened. The converted scripts each constructed their own
    #: `db.RAPIDDB()` — 12 direct constructions across the six — every one of
    #: which opens a second connection whose 33 mutating methods commit
    #: individually. A stage writing through its own handle could not be in one
    #: transaction with anything, so a failure midway left a partial effect
    #: with an attempt record claiming failure. Borrowing puts the stage's
    #: writes and the attempt's own lifecycle rows on one connection, where the
    #: transaction boundary is real.
    #:
    #: None in unit tests that construct a bare context and in job types that
    #: touch no database; `require_connection` is what turns that into one
    #: named failure rather than an AttributeError inside a query.
    connection: Any = None

    # -- product keys --------------------------------------------------------

    def product_prefix(self) -> str:
        """The S3 key prefix this attempt's products are uploaded under.

        **The one place product keys are built** (review finding #18). The
        prefix used to be `job_type/exposure/sca`, carrying neither run nor
        attempt identity — so reprocessing or retrying the same exposure/SCA
        OVERWROTE the earlier attempt's objects, and every old record and
        checksum then referred to keys whose bytes had changed. The storage
        design's immutable-keys rule is what that violates: a key, once
        written, names those bytes forever.

        Run and attempt identity make each attempt's products their own
        objects. The unit stays in the key because it is what a human looks
        for, and the attempt id goes last because it is the part that
        distinguishes two attempts at the same work.

        A context with no attempt identity — a unit test constructing a bare
        one — gets a prefix that says so rather than silently producing the
        old colliding shape, so a production path that lost its identity fails
        visibly instead of overwriting.

        The attempt component is zero-padded per the storage design's key
        schema (§ Key schema, component law: attempt 10 digits) — `unit.key`
        already carries its own padding, so this is the prefix's other
        numeric component.

        **REFUSED FOR DATABASE-EFFECT JOB TYPES** (co-design ruling 2).
        `submission.subjects` declares which job types are product-
        producing; every other job type's units are not exposure/SCA
        identity at all (a crossmatch unit's `.key` is a processing-date
        ordinal and a fixed SCA sentinel), so a product key built from it
        would be a real S3 path built from a synthetic value. Those job
        types write database effects through `context.record_effect`, never
        through `publish`, so this is a defect, not a legitimate call —
        raised here rather than left to build a misleading key.
        """
        from submission.subjects import UnknownJobType, is_product_producing

        try:
            product_producing = is_product_producing(self.job_type)
        except UnknownJobType:
            # A job type the typed-identity registry does not cover
            # (post-process, registration, reprocessing) is exposure/SCA-
            # shaped by construction and keeps building product keys as
            # every job type did before this ruling.
            product_producing = True
        if not product_producing:
            raise ConfigError(
                f"job type {self.job_type!r} is a database-effect job type "
                f"(co-design ruling 2): it declares an empty product set "
                f"and must never call product_prefix(). Its unit's "
                f".key is a synthetic carrier, not a storage identity — use "
                f"context.record_effect() instead.")
        if self.run_id is None or self.attempt_id is None:
            return f"{self.job_type}/{self.unit.key}/unidentified-attempt"
        return (f"{self.job_type}/{self.run_id}/{self.unit.key}"
                f"/attempt-{int(self.attempt_id):010d}")

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

    def product_role(self, role: str) -> str:
        """The published product this release bound to `role`.

        A stage that wants "the difference image" asks for the ROLE and gets
        back the product name the release nominated. Spelling an algorithm
        instead is the defect this exists to prevent: the binding is one knob
        in release content, and every consumer turns that one.
        """
        return science_config.product_role(self.science, role)

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

    # -- published products --------------------------------------------------
    #
    # THE SPLIT (review finding #18). `products` is the stage-to-stage channel:
    # downloaded inputs, scratch files and intermediates all live there, because
    # a later stage consumes them by name. The upload stage published EVERY
    # on-disk entry of it, and the terminal record serialized the same mapping
    # as local paths and scalars. So the record listed scratch paths beside real
    # outputs, with no way to tell which canonical S3 objects were final
    # products, and no URI or checksum to verify their bytes.
    #
    # A published product is a different thing from a stage output, and it is
    # now recorded as one: named, uploaded under a run/attempt-scoped key, and
    # carried into the terminal record as an immutable URI plus the checksum of
    # the bytes that were uploaded. Registration reads those entries; it never
    # has to guess which of a stage's filenames mattered.

    def publish(self, name: str, uri: str, checksum: str,
                size: int | None = None,
                product_type: str | None = None) -> dict:
        """Record that `name` was uploaded to `uri` with `checksum`.

        Called by the upload stages after the bytes are durable — never before,
        because an entry here asserts the object exists. Returns the entry.
        """
        entry = {"uri": uri, "checksum": checksum}
        if size is not None:
            entry["size"] = size
        if product_type is not None:
            entry["product_type"] = product_type
        self.published_products[name] = entry
        return entry

    def publishable(self) -> list:
        """The `products` entries that are real files, as (name, path) pairs.

        The upload stages' one source of truth for what to upload. Entries that
        are not paths on disk — infobits, counts, resolved identities — are
        provenance, not products, and are recorded through `record`.
        """
        import os
        return [(name, value) for name, value in sorted(self.products.items())
                if isinstance(value, str) and os.path.isfile(value)]

    def record(self, **facts: Any) -> None:
        """Add facts to the attempt's provenance.

        Checksums, infobits, source counts: what the terminal record should
        carry about what this attempt produced. Kept separate from `products`
        because products are filenames the next stage consumes, and these are
        values the record consumes.
        """
        self.provenance.update(facts)

    def require_connection(self):
        """The attempt's borrowed database connection, required.

        Raises
        ------
        ConfigError
            If the entrypoint did not lend one. A database-effect job type
            without a connection is a wiring fault in this image, not a bad
            submission — the route matrix already assigned the job type a
            lane, so a missing connection means the entrypoint failed to pass
            what it opened.
        """
        if self.connection is None:
            raise ConfigError(
                f"job type {self.job_type!r} needs the attempt's database "
                f"connection and none was lent to this context. The "
                f"entrypoint opens one per attempt on the route's lane and "
                f"passes it here; a stage never opens its own.")
        return self.connection

    def record_effect(self, rows_written: int = 0, rows_removed: int = 0,
                      **extra: Any) -> None:
        """Record this unit's DATABASE EFFECT in the attempt's provenance.

        **The terminal record of a database-effect job type is a pure
        disposition record** (co-design ruling 2, operations design § Post-DB
        science chain): it declares an empty product set, promotes nothing,
        "and its effect — rows written, rows removed — is recorded in the
        attempt record's own fields".

        So this is where the work becomes visible. A catalog-load unit that
        loaded 4.1M rows and one that loaded none both close successfully and
        both are honest; the difference is here, and it is what makes an empty
        result reviewable rather than indistinguishable from a no-op.

        Counts ACCUMULATE across the stages of one unit rather than
        overwriting, because a sequence may write through more than one stage
        and the unit's effect is their sum. Passing a count of zero is
        meaningful and is kept: it records that the stage ran and found
        nothing, which is exactly what the should-find-nothing dedup check
        exists to say.
        """
        self.provenance["rows_written"] = (
            self.provenance.get("rows_written", 0) + int(rows_written))
        self.provenance["rows_removed"] = (
            self.provenance.get("rows_removed", 0) + int(rows_removed))
        if extra:
            self.provenance.update(extra)

    # -- derived paths -------------------------------------------------------

    def scratch(self, *parts: str) -> str:
        """A path for an intermediate file. Dies with the container."""
        return self.workdir.scratch(*parts)

    def bundle(self, *parts: str) -> str:
        """A path for evidence. Uploaded with the diagnostics bundle."""
        return self.workdir.bundle_path(*parts)
