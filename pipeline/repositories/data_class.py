"""The data-class provenance read (migration 090), over a caller's connection.

One question: what data class(es) do the admission manifests covering these
L2 inputs record? That is the read side of the provenance chain the
operations design describes — "work units, attempts, products, alerts, and
catalog rows inherit their data class from input identities through the
provenance chain" — and gathering asks it once per unit to inherit a class
onto `work_units.data_class`.

**WHY A REPOSITORY AND NOT A `RAPIDDB` METHOD.** `RAPIDDB` is frozen (rule
17; brief G's ratified merge decision), and an earlier revision of THIS
change added `get_data_classes_for_l2files` straight to it and was correctly
refused by `pipeline/contract/test_deletion_exclusivity.py`. That test's own
message records why the assertion exists: "The D, F and E workers each broke
this and each needed a fix round." This was the fourth occurrence, caught by
the mechanism built for it rather than at a merge gate.

The substance of the rule matters here as much as the letter. `RAPIDDB`'s
methods report failure by setting `exit_code` and returning `None` — a
silence a caller must remember to check. For THIS query that failure mode is
particularly bad: a swallowed error returns "no classes recorded", which is
indistinguishable from the legitimate answer for an input with no admission
manifest, and the caller resolves both to "no class". So a transient query
failure would silently file a unit's products under the fallback class
instead of its real one. A typed `RepositoryQueryFailed` cannot be mistaken
for an answer.

**THE SET, NOT A VALUE.** A unit's inputs may span manifests of different
classes, and the design's rule for that case is "a mixed derivation takes
the most restrictive class of any input". Combining is
`submission.data_class.most_restrictive`'s job — a decision about science
authorization, tested on its own terms — so this returns every distinct
class it finds and decides nothing. A `max()` or a `LIMIT 1` here would pick
a class by collation or by physical row order and be wrong in exactly the
case the rule exists for.

**NULLs ARE EXCLUDED, NOT RETURNED.** A manifest ingested before 090 records
no class and contributes no evidence about what a unit is. An empty result
therefore means "nothing covering these inputs knows", which the caller
resolves to no class at all rather than to a guess.
"""

from pipeline.repositories.errors import RepositoryQueryFailed

#: `admission_l2files.rid` -> `manifest_id` -> `admission_manifests`. The join
#: the provenance chain names, and the only one: `admission_l2files` is the
#: grain at which an L2 file is admitted, and its `manifest_id` is what ties
#: it to the ingest run whose substrate and injection were fixed at creation.
_DATA_CLASSES_SQL = (
    "SELECT DISTINCT m.data_class"
    "  FROM admission_l2files a"
    "  JOIN admission_manifests m ON m.manifest_id = a.manifest_id"
    " WHERE a.rid = ANY(%s)"
    "   AND m.data_class IS NOT NULL"
)


class DataClassRepository:
    """The provenance read, over a connection the caller owns.

    Never opens a connection, never commits — the established shape for this
    package (see `AssociationRepository`, `DiffImageRepository`).
    """

    def __init__(self, conn):
        self._conn = conn

    def _execute(self, method, statement, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall()
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise RepositoryQueryFailed(method, str(exc)) from exc

    def classes_for_l2files(self, rids):
        """Every DISTINCT data class the manifests covering `rids` record.

        Empty when nothing does — an input registered by a path that writes
        no admission manifest, or admitted before 090 added the column. That
        emptiness is an answer ("no manifest knows"), not a failure; a
        failure raises.
        """
        wanted = [int(rid) for rid in rids]
        if not wanted:
            return []
        rows = self._execute("classes_for_l2files", _DATA_CLASSES_SQL,
                             (wanted,))
        return [row[0] for row in rows or ()]
