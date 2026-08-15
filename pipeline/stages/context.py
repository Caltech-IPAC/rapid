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

#: The closed set of data classes an object key may be filed under — the
#: LEADING component of every object key.
#:
#: The two axes are independent and both matter to a consumer: whether the
#: pixels came from the telescope or a simulator (`real` / `sim`), and
#: whether sources were injected into them (`pristine` / `injected`). A
#: single flat token for the pair keeps the key's first component one
#: component, which is what makes an S3 prefix listing separable by it.
#:
#: CLOSED, AND VALIDATED ON EVERY BUILD. An unrecognized token would file
#: real products under a prefix nothing lists and nothing garbage-collects,
#: so `product_prefix()` refuses rather than passing the value through.
DATA_CLASSES = ("real-pristine", "real-injected",
                "sim-pristine", "sim-injected")


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
    #: This attempt's identity, for object keys (review finding #18). Set by
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

    # -- object keys ---------------------------------------------------------

    def product_prefix(self) -> str:
        """The S3 key prefix this attempt's products are uploaded under.

        **The one place object keys are built** (review finding #18). The
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

        **THE DATA CLASS LEADS THE KEY**, ahead of the job type:
        ``{data_class}/{job_type}/{run_id}/{unit.key}/attempt-{id:010d}``.
        It is first because it is the coarsest cut anything makes over this
        bucket — simulated pixels and real ones are never mixed in a listing,
        a lifecycle rule, or a bucket policy, and only a LEADING component
        makes an S3 prefix separable that way. Put it anywhere later and
        every consumer has to list the whole tree and filter.

        **THE CLASS IS THE UNIT'S OWN, AND THE PARAMETER IS THE FALLBACK.**
        The data class is a property of the DATA, so it belongs on the work
        unit: it is fixed at admission, recorded on `admission_manifests`,
        inherited onto `work_units.data_class` at gathering (taking the most
        restrictive class where a unit's inputs span manifests), carried in
        the unit's payload, and read HERE as a fact. That plumbing —
        migration 090, the gatherers, the payload schema — is what this read
        now depends on.

        The deployment-wide `data/class` parameter that used to be the only
        source is kept as a FALLBACK, not retired. It was correct only while
        a deployment did not mix classes, which is why it was always
        labelled a stopgap; but units gathered before the carrier existed
        have no fact to read, and retiring the parameter would leave them
        with no class at all. So a fact wins where there is one, and the
        parameter answers where there is not. A deployment that genuinely
        mixes classes is now served correctly for every unit gathered since
        090, which is the property the stopgap could not provide.

        `pipeline/gc/references.py`'s `canonical_prefix()` mirror moves with
        every change to this grammar, and it did with this one: it reads the
        same class from `work_units.data_class` through `attempt_facts()`,
        and reconstructs the pre-data-class shape when that column is NULL —
        which is how objects written before any of this stay attributable.

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
        ordinal and a fixed SCA sentinel), so an object key built from it
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
            # (registration, reprocessing) is exposure/SCA-shaped by
            # construction and keeps building object keys as every job
            # type did before this ruling.
            product_producing = True
        if not product_producing:
            raise ConfigError(
                f"job type {self.job_type!r} is a database-effect job type "
                f"(co-design ruling 2): it declares an empty product set "
                f"and must never call product_prefix(). Its unit's "
                f".key is a synthetic carrier, not a storage identity — use "
                f"context.record_effect() instead.")
        # AFTER the product-producing check, deliberately. A database-effect
        # job type must fail with its own message above — it has no business
        # building a key at all, and a missing-parameter error here would
        # misdescribe that defect as a deployment misconfiguration.
        #
        # THE UNIT'S OWN CLASS FIRST, THE PARAMETER ONLY AS FALLBACK. The
        # fact is the finished design — the data class is a property of the
        # DATA, carried from admission through `work_units.data_class`
        # (migration 090) into this unit's payload. The parameter is the
        # stopgap it replaces, and it is kept rather than retired because
        # units gathered BEFORE the carrier existed have no fact to read and
        # must keep filing under the class their deployment declared.
        # Retiring it would strand exactly those units; the fallback costs
        # one branch and the interim path's own documented behaviour is what
        # it preserves.
        data_class = self.optional_fact("data_class")
        source = "the unit's data_class fact"
        if data_class is None:
            data_class = self.parameter("data/class")
            source = ("the parameter tree's data/class (this unit carries "
                      "no data_class fact of its own)")
        if data_class not in DATA_CLASSES:
            raise ConfigError(
                f"{source} is {data_class!r}, which is not a data class; it "
                f"must be one of {', '.join(DATA_CLASSES)}. The data class "
                f"is the LEADING component of every object key, so an "
                f"unrecognized value would file this attempt's objects under "
                f"a prefix nothing lists and nothing collects.",
                parameter="data/class")
        # The degraded branch carries it too: a context that lost its attempt
        # identity is still real data of a known class, and filing it outside
        # the class tree would put it where no consumer of that class looks.
        if self.run_id is None or self.attempt_id is None:
            return (f"{data_class}/{self.job_type}/{self.unit.key}"
                    f"/unidentified-attempt")
        return (f"{data_class}/{self.job_type}/{self.run_id}/{self.unit.key}"
                f"/attempt-{int(self.attempt_id):010d}")

    # -- per-invocation facts ------------------------------------------------

    @property
    def facts(self) -> Any:
        """This unit's per-invocation facts — its TYPED payload.

        Named `facts` because that is what every stage calls them and what
        `fact()`/`optional_fact()` read. Since D4 there is one carrier, not
        a typed payload beside an all-optional facts object.
        """
        return self.unit.facts

    def fact(self, name: str) -> Any:
        """One per-invocation fact, required.

        Reads the unit's TYPED payload (`submission.payloads`), which is what
        `unit.facts` now names — D4 retired the all-optional `UnitFacts`
        object and moved its members onto the per-job-type payloads. Every
        call site is unchanged: they were already asking the right question,
        and the answer simply has a type now.

        **A NAME THIS JOB TYPE DOES NOT DECLARE IS ITS OWN FAILURE.** Under
        the old object every one of thirty members existed on every unit and
        defaulted to None, so `fact("psf_uri")` on a crossmatch unit and
        `fact("psf_uri")` on a science unit whose PSF lookup found nothing
        raised the identical message. They are different faults — the first
        is a coding error, the second a submission gap — and only the second
        is `input_missing`.

        Raises
        ------
        InputError
            If the manifest did not carry it. `input_missing` rather than
            `config_invalid`: the manifest is this invocation's input, and a
            missing fact means the submitter did not describe the unit fully —
            not that the deployment is misconfigured.
        """
        payload = self.unit.facts
        if not payload.declares(name):
            raise InputError(
                f"the job type {self.job_type!r} does not declare the fact "
                f"{name!r}; its payload declares "
                f"{sorted(set(payload.COMPONENTS) | set(payload.INVOCATION_FACTS))}. "
                f"Asking for a fact a job type has no place for is a coding "
                f"error, not a gap in the submission.",
                unit=self.unit.key, fact=name)
        value = getattr(payload, name, None)
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
