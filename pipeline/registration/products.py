"""The product registrar: operation-table rows for a reconciled attempt.

A PORT of the registration bodies from the four `__main__`-only scripts the
W6D cutover fence deleted (`registerCompletedJobsInDB.py` and its post-process
sibling, at `e03f22c^`). The call sequences and their argument orders are
theirs — `add_refimage` then `update_refimage` to set vbest, `register_refimcatalog`
per catalogue, `add_diffimage` then `update_diffimage`, then `register_diffimmeta` —
because those are the operations schema's contract and inventing a different
one would be a schema change wearing a refactor's clothes.

WHAT IS DELIBERATELY NOT PORTED is where the facts come from.

The legacy bodies read a per-job product `.ini` that the job itself had
uploaded, listed the product bucket by `<date>/jid<jid>/` prefix to discover
what existed, and grepped stdout logs for the exit code. All three are mutable
external state read after the fact: the `.ini` could be rewritten, the bucket
listing reflects whatever is there NOW rather than what this attempt produced,
and a log is not a record. Every fact here instead comes from the attempt's
own terminal record — immutable, checksummed, keyed by attempt identity, and
already validated by the reconciler before this can run.

**Where a fact the legacy body needed is ABSENT from the record, that is a
finding, not a gap to fill by guessing.** `MissingRecordFact` names it, the
registration fails, the attempt stays a candidate, and the fix is to extend
what the record carries through the provenance path that authors it — never to
reconstruct the value from a bucket listing or a config file. Silently
substituting a default is how the old chain registered products of failed runs.
"""

import logging

from pipeline.registration import products_identity
from pipeline.runtime.science_config import DIFFERENCE_IMAGE_ROLE
from submission.routes import JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE

logger = logging.getLogger("rapid.registration.products")

#: Catalogue types, as `register_refimcatalog` takes them. The legacy body
#: registered the SExtractor and PhotUtils reference catalogues under distinct
#: cattypes, and the PhotUtils one only where it had actually been uploaded.
#: `refimcatalogs.cattype` is a **smallint** in the deployed schema
#: (006-core-tables.sql:566), and `registerRefImCatalog` casts its third
#: argument to smallint. These were the STRINGS "sextractor" and "photutils",
#: which PostgreSQL refused outright:
#:
#:     InvalidTextRepresentation: invalid input syntax for type smallint:
#:     "sextractor"
#:
#: so every reference-image registration failed at its first catalogue. Found
#: by the FixD live mini-chain probe, and findable only there: the unit suite's
#: fake database accepts whatever it is handed, so a type the real column
#: cannot hold looks identical to one it can.
#:
#: PROPOSED VALUES, and they need Ben's ratification (recorded in the FixD
#: ledger and the disposition page). The legacy body read these from
#: `product_config['REF_IMAGE'][...cattype]` — a per-job product config that
#: the W6 cutover deleted and that never lived in this repo — and neither
#: repo carries a lookup table, a CHECK constraint, or a seeded vocabulary
#: for the column. So the numbering below is an ORDERING chosen here (1 =
#: SExtractor, the catalogue every reference image has; 2 = PhotUtils, the
#: one registered only where it was produced), not a value recovered from
#: the operations schema. It is internally consistent and it registers, but
#: if the archive already numbers these differently, this is where to fix it
#: — one constant each, and `refimcatalogspk (rfid, ppid, cattype)` means a
#: wrong number collides rather than silently duplicating.
CATTYPE_SEXTRACTOR = 1
CATTYPE_PHOTUTILS = 2


class MissingRecordFact(KeyError):
    """A fact the registration needs is not in the terminal record.

    Raised rather than defaulted. The registration consumer counts it as a
    failure, so the attempt remains a candidate and nothing is written from a
    half-known account — and the message names the field, so the fix is to
    extend the record's content rather than to guess here.
    """

    def __init__(self, field, attempt_id=None, where=None):
        self.field = field
        self.attempt_id = attempt_id
        super().__init__(
            f"the terminal record for attempt {attempt_id} carries no "
            f"{field!r}{f' in {where}' if where else ''}. Registration reads "
            f"the record and nothing else — the fix is to record this fact "
            f"through the path that authors it, not to reconstruct it from "
            f"the product bucket or a config file.")


def _need(record, field, attempt_id=None, where=None):
    value = record.get(field)
    if value is None:
        raise MissingRecordFact(field, attempt_id=attempt_id, where=where)
    return value


def published(record, attempt_id=None):
    """The record's published products, by name.

    Sequence 0 carries `products` as a LIST of named entries (that is what
    `termination._product_entries` builds), each with the immutable S3 URI and
    the checksum of the bytes uploaded. Indexing them by name is what lets the
    bodies below ask for one product without caring about list order.
    """
    entries = record.get("products")
    if not entries:
        raise MissingRecordFact("products", attempt_id=attempt_id)
    return {entry["name"]: entry for entry in entries if entry.get("name")}


def _product(products, name, attempt_id=None):
    entry = products.get(name)
    if entry is None:
        raise MissingRecordFact(f"products[{name!r}]", attempt_id=attempt_id)
    return entry


def role_product(record, science, role, attempt_id=None, fallback_roles=None):
    """The published product the attempt's release bound to `role`.

    A role is a stable contract name; the release binds it to the concrete
    product that fills it, and the attempt records the binding it resolved
    (`entrypoints/job.py`, beside the release digest). Reading it back from
    the record is what keeps registration a reader of records and nothing
    else — and what makes a replay resolve the role the way the original
    attempt did, even if a later release rebinds it.

    RECORDS AUTHORED BEFORE THE BINDING EXISTED. Every attempt published
    before 2026-08-08 carries the three difference images under their
    algorithm names and no `product_roles` at all, because nothing wrote
    one — and the ruling requires exactly those attempts to register on
    replay, since the refusal is why `diffimages` is empty. `fallback_roles`
    is that one narrow path: the caller passes the RUNNING release's
    bindings, used only when the record carries none, and the caller is
    told which happened so the ledger can say so. It is not a default —
    an unbound role still raises, and a record that DOES carry a binding
    always wins, so a replay of a modern attempt can never be answered by
    whatever release happens to be running.

    Returns (entry, product_name, resolved_from) where `resolved_from` is
    "record" or "release".
    """
    roles = record.get("product_roles") or science.get("product_roles")
    resolved_from = "record"
    if not roles:
        if not fallback_roles:
            raise MissingRecordFact("product_roles", attempt_id=attempt_id)
        roles = fallback_roles
        resolved_from = "release"
    bound = roles.get(role)
    if not bound:
        raise MissingRecordFact(f"product_roles[{role!r}]",
                                attempt_id=attempt_id)
    entry = _product(published(record, attempt_id), bound, attempt_id)
    return entry, bound, resolved_from


def register_reference_image(dbh, record, science, attempt_id=None,
                             record_sequence=None, identity_repository=None):
    """The reference-image body. (Legacy `registerCompletedJobsInDB.py`.)

    Order is the legacy order and it matters: `add_refimage` inserts the row
    and hands back the rfid and version, `update_refimage` then finalizes it so
    `vbest = 1` points at this version. Splitting those was the legacy design —
    the insert cannot know it is best until it exists — and the catalogues that
    follow are keyed by the rfid it returned.

    `record_sequence` is the attempt's `terminal_record_sequence`, and together
    with `attempt_id` it is what makes the insert idempotent under replay
    (migration 018, round-3 finding #8). The pair travels down to `addRefImage`,
    which finds-or-inserts on it before minting a version: registering the same
    attempt twice at the same sequence returns the row it already has, while a
    supersession at a higher sequence still mints a new version. Both are
    optional here only so a caller mid-port is not broken — production always
    has them, because the candidate query selects both columns.
    """
    products = published(record, attempt_id)
    image = _product(products, "reference_image", attempt_id)

    ppid = _need(record, "ppid", attempt_id)
    field = _need(science, "field", attempt_id, where="science_provenance")
    fid = _need(science, "fid", attempt_id, where="science_provenance")
    hp6 = _need(science, "hp6", attempt_id, where="science_provenance")
    hp9 = _need(science, "hp9", attempt_id, where="science_provenance")
    infobits = science.get("reference_image_infobits", 0)
    status = science.get("reference_image_status", 1)

    dbh.add_refimage(ppid, field, fid, hp6, hp9, infobits, status,
                     image["uri"], image["checksum"],
                     attempt_id, record_sequence)
    _check(dbh, "add_refimage", attempt_id)

    rfid = dbh.rfid
    version = dbh.version

    # Finalize, so vbest points at this version. Filename, checksum, infobits
    # and status are unchanged — the legacy comment says exactly this.
    dbh.update_refimage(rfid, image["uri"], image["checksum"], status, version)
    _check(dbh, "update_refimage", attempt_id)

    registered = {"rfid": rfid, "version": version, "catalogs": []}

    for name, cattype in (("reference_sexcat", CATTYPE_SEXTRACTOR),
                          ("reference_psfcat", CATTYPE_PHOTUTILS)):
        entry = products.get(name)
        if entry is None:
            # NOT an error: the PhotUtils catalogue is registered only where it
            # was actually produced and uploaded, which is the legacy body's
            # `if photutils_refimage_catalog_uploaded_to_bucket:` guard. An
            # absent entry means the upload stage did not publish one, and the
            # published list is the authority on that.
            logger.info("attempt %s published no %s; not registering one",
                        attempt_id, name)
            continue
        dbh.register_refimcatalog(
            rfid, ppid, cattype, field, hp6, hp9, fid,
            science.get(f"{name}_status", 1), entry["uri"], entry["checksum"])
        _check(dbh, "register_refimcatalog", attempt_id)
        registered["catalogs"].append({"cattype": cattype,
                                       "rfcatid": dbh.rfcatid,
                                       "svid": dbh.svid})

    # THE IDENTITY MODEL (rule 10), written inside this same transaction.
    # Additive: everything above is unchanged and every legacy column stays
    # populated exactly as before, because the production reader set is
    # broader than the registration writers. What this adds is the product
    # row keyed by a deterministic digest, one artifact row per published
    # file, and the binding between them.
    #
    # `identity_repository is None` is the pre-rollout path and the unit
    # suites' path — the legacy registration is complete on its own, and
    # was for as long as this module has existed.
    if identity_repository is not None:
        identity = products_identity.register_reference_identity(
            identity_repository, record, science, attempt_id,
            record_sequence, rfid, version)
        if identity is not None:
            registered["identity"] = identity

    logger.info("attempt %s registered reference image rfid=%s version=%s "
                "with %d catalog(s)", attempt_id, rfid, version,
                len(registered["catalogs"]))
    return registered


def register_difference_image(dbh, record, science, attempt_id=None,
                              record_sequence=None, fallback_roles=None,
                              identity_repository=None):
    """The difference-image body. (Legacy `registerCompletedJobsInDB.py`.)

    Same shape as the reference body — `add_diffimage`, then `update_diffimage`
    to set vbest, then `register_diffimmeta` for the ZOGY measurements keyed by
    the pid the insert returned.

    The sky position is required rather than defaulted: `add_diffimage` takes
    the tile centre and all four corners, and a difference image registered
    with a partial or zeroed footprint is one that later spatial queries will
    silently mis-match.

    `record_sequence` carries the same idempotence as in the reference body:
    the (attempt_id, sequence) pair reaches `addDiffImage`, which finds-or-
    inserts on it rather than blindly minting `max(version)+1`. That is what
    stops a replayed pass from writing a second difference image for work that
    was already registered — and it is only half the fix, because the rows and
    the watermark still have to commit together; see `registrar`.
    """
    # THE ROLE, NOT AN ALGORITHM. The payload publishes three difference
    # images per attempt; the release binds the difference-image role to the
    # one that registers, and the attempt recorded that binding. Asking for a
    # literal here is what refused promotion on every real science attempt
    # (decisions.md § Difference-image product vocabulary): the reader's
    # vocabulary and the record author's were two different things.
    difference, difference_product, role_source = role_product(
        record, science, DIFFERENCE_IMAGE_ROLE, attempt_id,
        fallback_roles=fallback_roles)

    ppid = _need(record, "ppid", attempt_id)
    rid = _need(science, "rid", attempt_id, where="science_provenance")
    rfid = _need(science, "reference_image_id", attempt_id,
                 where="science_provenance")
    field = _need(science, "field", attempt_id, where="science_provenance")
    fid = _need(science, "fid", attempt_id, where="science_provenance")
    sca = _need(science, "sca", attempt_id, where="science_provenance")
    hp6 = _need(science, "hp6", attempt_id, where="science_provenance")
    hp9 = _need(science, "hp9", attempt_id, where="science_provenance")

    position = science.get("sky_position") or {}
    corners = []
    for key in ("ra0", "dec0", "ra1", "dec1", "ra2", "dec2", "ra3", "dec3",
                "ra4", "dec4"):
        if position.get(key) is None:
            raise MissingRecordFact(f"sky_position[{key!r}]",
                                    attempt_id=attempt_id,
                                    where="science_provenance")
        corners.append(position[key])

    infobits_sci = science.get("diffimage_infobits", 0)
    infobits_ref = science.get("reference_image_infobits", 0)
    status = science.get("difference_image_status", 1)

    dbh.add_diffimage(rid, ppid, rfid, infobits_sci, infobits_ref, *corners,
                      status, difference["uri"], difference["checksum"],
                      attempt_id, record_sequence)
    _check(dbh, "add_diffimage", attempt_id)

    pid = dbh.pid
    version = dbh.version

    dbh.update_diffimage(pid, difference["uri"], difference["checksum"],
                         status, version)
    _check(dbh, "update_diffimage", attempt_id)

    # The ZOGY measurements. Every one is a value the science stages computed
    # and recorded; none has a meaningful default, so an absent one is a
    # finding about what the record carries.
    meta = [_need(science, name, attempt_id, where="science_provenance")
            for name in ("nsexcatsources", "scalefacref", "dxrmsfin",
                         "dyrmsfin", "dxmedianfin", "dymedianfin")]

    dbh.register_diffimmeta(pid, fid, sca, field, hp6, hp9, *meta)
    _check(dbh, "register_diffimmeta", attempt_id)

    # The product that filled the role is logged and returned, not just the
    # pid: an operator reading either has to be able to tell WHICH difference
    # image the row points at without going back to the release content.
    result = {"pid": pid, "version": version,
              "product": difference_product,
              "role_resolved_from": role_source}

    # THE IDENTITY MODEL (rule 10), in this same transaction. See the
    # reference body's note; the difference here is that the product key is
    # COMPOSITIONAL — it digests the reference image by the reference's own
    # product key, so a difference image cannot get an identity until its
    # reference has one. `register_difference_identity` returns None in that
    # case and registration proceeds legacy-only, which is the correct
    # behaviour during rollout: an invented key would be worse than none.
    #
    # `difference_product` is the published NAME that filled the role — the
    # artifact that realizes this product. The other two published
    # difference images become artifacts with no product row, exactly as
    # `cdf/science/pipeline.toml:73-75` records the design.
    if identity_repository is not None:
        identity = products_identity.register_difference_identity(
            identity_repository, record, science, attempt_id,
            record_sequence, pid, version, difference_product)
        if identity is not None:
            result["identity"] = identity

    logger.info("attempt %s registered difference image pid=%s version=%s "
                "(role %s ← %s, resolved from the %s)", attempt_id, pid,
                version, DIFFERENCE_IMAGE_ROLE, difference_product,
                role_source)
    return result


def _check(dbh, call, attempt_id):
    """The legacy `if dbh.exit_code >= 64: exit(...)` guard, as an exception.

    The scripts called `exit()` from inside the registration body, which took
    the whole process down mid-pass and left every later job unregistered with
    no account of how far it got. Raising instead lets the consumer count this
    one attempt as failed and carry on — and leaves it a candidate, because the
    watermark only advances after a registration that returned.
    """
    code = getattr(dbh, "exit_code", 0)
    if code >= 64:
        raise RegistrationFailed(
            f"{call} failed for attempt {attempt_id}: rapid_db exit_code "
            f"{code}")


class RegistrationFailed(RuntimeError):
    """A database call in a registration body reported failure.

    Raised by `_check` when `dbh.exit_code >= 64`, which `rapid_db.py` also
    sets for a genuine constraint conflict on the `update*` calls (catalog.md
    § Promotion, "Conflicts": the natural-unique constraints and the partial
    `vbest` indexes are both RETRYABLE — the attempt stays a candidate and a
    later pass's registration legitimately supersedes the earlier winner).
    `exit_code` does not distinguish a conflict from a transient database
    fault, so neither can this class: both are infrastructure-shaped and
    retried next pass, never a durable rejection. `RecordValidationRejected`
    below is the narrower, unambiguous subset — never raised by `_check`.
    """


class RecordValidationRejected(RegistrationFailed):
    """The terminal record itself fails verification (integration ruling 4).

    Raised only by `read_record`'s own checks — checksum mismatch, attempt-
    identity mismatch — where the record's bytes are what is wrong, not a
    database call. Re-running the same registration body against the same
    immutable record reaches the same verdict, which is what makes this a
    DURABLE rejection (catalog.md § Promotion, "a validation rejection
    commits its own registration-outcome entry ... without advancing the
    registration watermark") rather than a retryable failure. A subclass of
    `RegistrationFailed`, not a sibling: any caller that already catches the
    parent broadly still catches this.
    """


def read_record(store, row):
    """Fetch and validate the attempt's terminal record.

    The row cites the record by key AND checksum, and both are checked here.
    Registration acts on what the record says, so reading it without verifying
    the bytes would reintroduce exactly the trust-the-external-state problem
    the record exists to remove — an object silently replaced at a known key
    would be registered as though the attempt had produced it.

    A row whose citation is incomplete is a finding: the reconciler's own
    materialization is what supplies key and checksum in the crash case
    (review finding #14), so a NULL here means that has not happened yet and
    the attempt is not ready to register.
    """
    import json

    from pipeline.runtime.boundaries import checksum as body_checksum

    attempt_id = row.get("attempt_id")
    key = row.get("terminal_record_key")
    if not key:
        raise MissingRecordFact("terminal_record_key", attempt_id=attempt_id)

    raw = store.get(key)
    recorded = row.get("terminal_record_checksum")
    computed = body_checksum(raw)
    # A NULL checksum is the incomplete citation the docstring describes, and
    # it was silently ACCEPTED here — `if recorded and ...` skipped
    # verification entirely, so the one row shape least entitled to trust got
    # the least checking. Enforced as the missing fact it is (round-3 #1).
    if not recorded:
        raise MissingRecordFact("terminal_record_checksum",
                                attempt_id=attempt_id)
    if recorded != computed:
        raise RecordValidationRejected(
            f"the terminal record at {key} for attempt {attempt_id} does not "
            f"match the checksum the row cites (recorded {recorded}, computed "
            f"{computed}); refusing to register from bytes the attempt did "
            f"not write")

    body = json.loads(raw.decode("utf-8"))
    if str(body.get("attempt_id")) != str(attempt_id):
        raise RecordValidationRejected(
            f"the record at {key} belongs to attempt "
            f"{body.get('attempt_id')}, not {attempt_id}")
    return body


def registrar(dbh, store, fallback_roles=None, identity_repository=None):
    """Build the `register(row, verdict)` callback the consumer injects.

    `store` is the records store: the registrar fetches each attempt's terminal
    record itself and validates it, rather than being handed a body. That is
    the point of the whole design — registration reads the immutable record.

    `dbh` may be a handle or a CALLABLE returning one. A callable is what the
    entrypoint passes, so the database connection is opened on the first
    attempt that actually registers rather than at wiring time: a pass with no
    candidates, or one whose every candidate is refused by the taxonomy, then
    costs no connection at all — and resolving the registrar cannot fail for
    want of a database in a context that was never going to use one.

    Dispatch is on the JOB TYPE recorded in the attempt's own record, because
    what registering means differs by type: a reference-image attempt
    registers a reference and its catalogues, a science attempt registers a
    difference image and its measurements.

    `fallback_roles` is the running release's product-role bindings, used
    ONLY for records authored before bindings were recorded at all — see
    `role_product`. Absent it, such a record refuses, which is the correct
    behaviour everywhere except a deliberate replay of pre-binding attempts.
    """
    handle = {"value": None if callable(dbh) else dbh}

    def resolve():
        if handle["value"] is None:
            handle["value"] = dbh()
        return handle["value"]

    def register(row, verdict, record=None):
        attempt_id = row.get("attempt_id")
        body = record if record is not None else read_record(store, row)

        # A record carrying NO job type is still a missing fact, and is checked
        # before the registrable gate: "the record does not say what this was"
        # and "the record says what this was and it registers nothing" are
        # different findings, and only the first means the record is
        # incomplete. Records written before `job_type` was threaded (round 2)
        # are the ones that land here.
        if not body.get("job_type"):
            raise MissingRecordFact("job_type", attempt_id=attempt_id)

        # Filtered here, where the record body is first in hand, because the
        # candidate query cannot filter on a column the attempts table does not
        # have. Returning None rather than raising is what stops an
        # unregistrable type from counting as a failure and staying a candidate
        # forever — the consumer treats a return as success and advances the
        # watermark, which is the correct account: there was nothing to
        # register and that question is now settled for this record sequence.
        if not is_registrable(body):
            logger.info(
                "attempt %s ran as job type %r, which produces no "
                "operations-table rows; nothing to register",
                attempt_id, body.get("job_type"))
            return None

        science = body.get("science_provenance") or {}
        # `row.get("job_type")` used to be a fallback here, to a column that
        # does not exist: the attempts table has no job_type and
        # `consumer._COLUMNS` selects none, so it read None on every row it was
        # ever asked. The record body is the only source, which is the design
        # anyway — registration reads the record and nothing else.
        job_type = body["job_type"]

        # The sequence comes off the ROW, not the record: it is the reconciler's
        # count of closure records published for this attempt, and the same
        # number the consumer is about to write as the watermark. Reading it
        # from one place is what makes "the row the registrar inserts and the
        # watermark the consumer writes describe the same registration" true by
        # construction rather than by two call sites agreeing.
        record_sequence = row.get("terminal_record_sequence")

        if job_type == JOB_TYPE_REFERENCE_IMAGE:
            return register_reference_image(
                resolve(), body, science, attempt_id, record_sequence,
                identity_repository=identity_repository)
        if job_type == JOB_TYPE_SCIENCE:
            return register_difference_image(
                resolve(), body, science, attempt_id, record_sequence,
                fallback_roles=fallback_roles,
                identity_repository=identity_repository)
        # Unreachable while `REGISTRABLE_JOB_TYPES` and the two branches above
        # agree — `is_registrable` has already returned for anything else. It
        # is kept as the guard for exactly that disagreement: a type added to
        # the registrable set without a body would otherwise fall off the end
        # of this function and return None, which the consumer would count as
        # a successful registration that wrote nothing and then advance the
        # watermark past it. Silent non-registration is the one outcome this
        # module exists to prevent.
        raise UnregistrableJobType(job_type, attempt_id=attempt_id)

    return register


#: The job types that have a registration body. A record whose job type is not
#: here describes work that produces no operations-table rows — a registration
#: pass over other attempts, a post-process stamping run — and is not a
#: candidate. See `is_registrable`.
REGISTRABLE_JOB_TYPES = frozenset({JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE})


class UnregistrableJobType(ValueError):
    """The record names a job type that has no registration body.

    Distinct from `MissingRecordFact`: the record is complete and says what
    kind of work it was, and that kind produces nothing to register. Raised
    rather than returned so a candidate that reached the registrar in this
    state is still counted — but `is_registrable` should have filtered it out
    upstream, so reaching this is itself the finding.
    """

    def __init__(self, job_type, attempt_id=None):
        self.job_type = job_type
        self.attempt_id = attempt_id
        super().__init__(
            f"attempt {attempt_id} ran as job type {job_type!r}, which has no "
            f"registration body: it produces no operations-table rows. "
            f"Registrable types are "
            f"{', '.join(sorted(REGISTRABLE_JOB_TYPES)) or '(none resolved)'}.")


def is_registrable(record):
    """Whether this attempt's record describes work a body can register.

    THE CANDIDATE FILTER (round-3 finding #7). The candidate query has no
    job-type predicate — it cannot have one, because the attempts table has no
    job_type column and `consumer._COLUMNS` selects none — so every reconciled
    attempt of every type arrived at the registrar. Registration and
    post-process attempts closed `(success, published)` just like science ones,
    and `success`+`published` is the sole pair `decide` registers on, so they
    became candidates the registrar could only refuse. Each refusal counted as
    a failure and left the attempt a candidate, so a successful registration
    pass poisoned every pass after it.

    The disposition fix in the entrypoint closes that at the source — those
    jobs now close `none`, which `decide` skips. This is the second gate, on
    the record's own statement of what it was, because the two answer different
    questions: the disposition says what an attempt produced, and this says
    whether the registrar knows what to do with it. Attempts closed before the
    disposition fix shipped are still out there carrying `published`, and this
    is what keeps them from failing forever.
    """
    return (record or {}).get("job_type") in REGISTRABLE_JOB_TYPES
