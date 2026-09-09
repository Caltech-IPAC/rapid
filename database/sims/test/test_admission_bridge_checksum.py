"""`source_checksum_from_head`: the S3 checksum a multipart ETag cannot be.

Observed live on rapid-admin, 2026-09-09 18:10 UTC: the g0005 admission run's
`db_register_socsim_files.py:1078-1089` enumerated every input under
`head["ETag"].strip('"')` as `algorithm="md5"`. A multipart-uploaded object's
ETag is `<md5-of-part-md5s>-<parts>` (every g0005 object is an 8-part upload,
59 MB each) — not an md5 of the content at all — and
`admission_identity.normalized_checksum` rightly refused all 9,911 of them:
"a md5 checksum is 32 hex characters; got 34".

What S3 actually offers for these objects (probed with `head-object
--checksum-mode ENABLED` and `get-object-attributes`):
`ChecksumCRC64NVME: "o99qjx7pbqY="` with `ChecksumType: FULL_OBJECT` — a true
content checksum, independent of part layout, unlike the ETag. This module
picks that up when it is present, and falls back to the ETag-as-md5 the
g0001 backfill relied on when the object is single-part (no full-object
checksum, and a bare 32-hex-character ETag that IS the content's md5).

Stub-tier: `admission_bridge` imports only from
`pipeline.repositories.admission`, which imports only
`pipeline.repositories.admission_identity` and the repository base — no
psycopg2, no boto3, no live connection needed to exercise this pure
function.
"""

import base64

import pytest

from database.sims.admission_bridge import source_checksum_from_head
from pipeline.repositories.admission_identity import AdmissionIdentityError

# The live g0005 example: an 8-part multipart upload, probed 2026-09-09.
MULTIPART_ETAG = '"53bd3f188e51a029312b60eeab18b7d8-8"'
MULTIPART_CRC64NVME_B64 = "o99qjx7pbqY="
EXPECTED_CRC64NVME_HEX = base64.b64decode(MULTIPART_CRC64NVME_B64).hex()

# The g0001 backfill case: single-part, so the ETag IS the content's md5.
SINGLE_PART_ETAG = '"9f3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c"'


def test_a_multipart_full_object_checksum_is_used_over_its_etag():
    """The live failing case: FULL_OBJECT CRC64NVME wins when present."""
    head = {
        "ETag": MULTIPART_ETAG,
        "ChecksumCRC64NVME": MULTIPART_CRC64NVME_B64,
        "ChecksumType": "FULL_OBJECT",
    }
    checksum, algorithm = source_checksum_from_head(head)
    assert algorithm == "crc64nvme"
    assert checksum == EXPECTED_CRC64NVME_HEX
    assert len(checksum) == 16


def test_a_single_part_object_falls_back_to_its_etag_as_md5():
    """No full-object checksum, and the ETag is bare 32-hex: the g0001 case."""
    head = {"ETag": SINGLE_PART_ETAG}
    checksum, algorithm = source_checksum_from_head(head)
    assert algorithm == "md5"
    assert checksum == "9f3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c"


def test_a_multipart_etag_with_no_full_object_checksum_is_refused():
    """A multipart ETag is never a content digest — never silently accepted.

    This is the shape a bucket policy or SDK version without checksum
    support could produce: `ChecksumMode="ENABLED"` was not honoured, or the
    upload predates S3 attaching one. Silently hashing the ETag here would
    reintroduce the exact defect this module exists to fix.
    """
    head = {"ETag": MULTIPART_ETAG}
    with pytest.raises(AdmissionIdentityError) as caught:
        source_checksum_from_head(head)
    message = str(caught.value)
    assert "ETag" in message
    assert MULTIPART_ETAG.strip('"') in message


def test_a_checksum_type_that_is_not_full_object_is_not_trusted():
    """`ChecksumType` other than FULL_OBJECT is a per-part checksum, not a
    content digest either — falling through to the ETag rule is correct only
    because this fixture's ETag is itself multipart and gets refused."""
    head = {
        "ETag": MULTIPART_ETAG,
        "ChecksumCRC64NVME": MULTIPART_CRC64NVME_B64,
        "ChecksumType": "COMPOSITE",
    }
    with pytest.raises(AdmissionIdentityError):
        source_checksum_from_head(head)
