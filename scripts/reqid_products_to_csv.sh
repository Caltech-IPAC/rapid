#!/bin/bash
#
# Write a CSV summary of the science-pipeline products of one processing request (reqid):
# observation identifiers, L2-file metadata, and the S3 directories of the reference and
# difference images.
#
# Usage:  reqid_products_to_csv.sh <reqid> <output.csv>
#
# Database connection from the same environment variables as the RAPID pipeline:
# DBSERVER, DBPORT, DBNAME, DBUSER, and DBPASS (or a ~/.pgpass entry).

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <reqid> <output.csv>" >&2
    exit 64
fi

reqid=$1
output_csv=$2

if ! [[ $reqid =~ ^[0-9]+$ ]]; then
    echo "*** Error: reqid must be a positive integer (got '$reqid')" >&2
    exit 64
fi

for var in DBSERVER DBPORT DBNAME DBUSER; do
    if [ -z "${!var:-}" ]; then
        echo "*** Error: Env. var. $var not set" >&2
        exit 64
    fi
done

if [ -n "${DBPASS:-}" ]; then
    export PGPASSWORD=$DBPASS
fi

# The difference image of each science job is the version this request produced: diffimages
# records no job or request, so it is matched on the request's S3 path, <date>/req<reqid>/jid<jid>/.

query="
copy (
    select e.program_id,
           e.execution_plan,
           e.pass,
           e.segment,
           e.observation,
           l.dateobs,
           l.mjdobs,
           l.field,
           l.sca,
           f.filter,
           l.exptime                                  as exposure_time,
           regexp_replace(r.filename, '/[^/]*\$', '') as reference_dir,
           regexp_replace(d.filename, '/[^/]*\$', '') as diff_dir,
           dm.nsexcatsources
    from jobs j
    join l2files l          on l.rid = j.rid
    join exposures e        on e.expid = l.expid
    join filters f          on f.fid = l.fid
    join diffimages d       on d.rid = j.rid
                           and d.ppid = j.ppid
                           and d.filename like '%/req${reqid}/%'
    left join refimages r   on r.rfid = d.rfid
    left join diffimmeta dm on dm.pid = d.pid
    where j.reqid = ${reqid}
      and j.ppid = 15
    order by l.dateobs, l.sca
) to stdout with (format csv, header)
"

psql -X -v ON_ERROR_STOP=1 -h "$DBSERVER" -p "$DBPORT" -d "$DBNAME" -U "$DBUSER" \
     -c "$query" > "$output_csv"

nrows=$(( $(wc -l < "$output_csv") - 1 ))
echo "Wrote $nrows rows for reqid=$reqid to $output_csv"
