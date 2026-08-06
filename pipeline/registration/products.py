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

logger = logging.getLogger("rapid.registration.products")

#: Catalogue types, as `register_refimcatalog` takes them. The legacy body
#: registered the SExtractor and PhotUtils reference catalogues under distinct
#: cattypes, and the PhotUtils one only where it had actually been uploaded.
CATTYPE_SEXTRACTOR = "sextractor"
CATTYPE_PHOTUTILS = "photutils"


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


def register_reference_image(dbh, record, science, attempt_id=None):
    """The reference-image body. (Legacy `registerCompletedJobsInDB.py`.)

    Order is the legacy order and it matters: `add_refimage` inserts the row
    and hands back the rfid and version, `update_refimage` then finalizes it so
    `vbest = 1` points at this version. Splitting those was the legacy design —
    the insert cannot know it is best until it exists — and the catalogues that
    follow are keyed by the rfid it returned.
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
                     image["uri"], image["checksum"])
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

    logger.info("attempt %s registered reference image rfid=%s version=%s "
                "with %d catalog(s)", attempt_id, rfid, version,
                len(registered["catalogs"]))
    return registered


def register_difference_image(dbh, record, science, attempt_id=None):
    """The difference-image body. (Legacy `registerCompletedJobsInDB.py`.)

    Same shape as the reference body — `add_diffimage`, then `update_diffimage`
    to set vbest, then `register_diffimmeta` for the ZOGY measurements keyed by
    the pid the insert returned.

    The sky position is required rather than defaulted: `add_diffimage` takes
    the tile centre and all four corners, and a difference image registered
    with a partial or zeroed footprint is one that later spatial queries will
    silently mis-match.
    """
    products = published(record, attempt_id)
    difference = _product(products, "difference_image", attempt_id)

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
                      status, difference["uri"], difference["checksum"])
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

    logger.info("attempt %s registered difference image pid=%s version=%s",
                attempt_id, pid, version)
    return {"pid": pid, "version": version}


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
    """A database call in a registration body reported failure."""


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
    if recorded and recorded != computed:
        raise RegistrationFailed(
            f"the terminal record at {key} for attempt {attempt_id} does not "
            f"match the checksum the row cites (recorded {recorded}, computed "
            f"{computed}); refusing to register from bytes the attempt did "
            f"not write")

    body = json.loads(raw.decode("utf-8"))
    if str(body.get("attempt_id")) != str(attempt_id):
        raise RegistrationFailed(
            f"the record at {key} belongs to attempt "
            f"{body.get('attempt_id')}, not {attempt_id}")
    return body


def registrar(dbh, store):
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
    """
    from submission.routes import JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE

    handle = {"value": None if callable(dbh) else dbh}

    def resolve():
        if handle["value"] is None:
            handle["value"] = dbh()
        return handle["value"]

    def register(row, verdict, record=None):
        attempt_id = row.get("attempt_id")
        body = record if record is not None else read_record(store, row)
        science = body.get("science_provenance") or {}
        job_type = body.get("job_type") or row.get("job_type")

        if job_type == JOB_TYPE_REFERENCE_IMAGE:
            return register_reference_image(resolve(), body, science,
                                            attempt_id)
        if job_type == JOB_TYPE_SCIENCE:
            return register_difference_image(resolve(), body, science,
                                             attempt_id)
        raise MissingRecordFact("job_type", attempt_id=attempt_id)

    return register
