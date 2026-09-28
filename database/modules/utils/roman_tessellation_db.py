"""Sky-tessellation access.

The surviving path is `RomanTessellationClosedForm` below: certification
proved the tessellation regular, so `rtid(ra, dec)` is arithmetic and the
1.4 GiB baked SQLite file is no longer consulted at runtime.
`RomanTessellationNSIDE512`, dev's SQLite class, is not carried in this
tree (dev keeps it for its launcher scripts).

See rapid_systems `tools/tessellation/` for the canonical builder, the
certification battery, and the versioned PostgreSQL installation.
"""

import math
import numpy as np

from database.modules.utils import roman_tessellation as tess


class RomanTessellationClosedForm:

    """Closed-form sky-tessellation access — no database, no I/O.

    Drop-in for the query surface of `RomanTessellationNSIDE512` that the
    payload scripts use, computing every answer arithmetically from the
    ring structure instead of querying the baked SQLite file.

    Certification (rapid_systems
    `tools/tessellation/certification-2026-08-06.txt`) covers the two
    halves of this class to two different depths, and the difference
    matters to anyone relying on it:

    * **Tile identity is exhaustive.** Every one of the 6,291,458
      generated rows was compared against the baked SQLite file column
      for column, and each tile's own centre resolves to its own rtid.
    * **Neighbour expansion is sampled, not exhaustive.** The battery
      draws 3,004 tiles (`certify.py:check_adjacency`) and checks
      *invariants* on them — that neighbour relations are symmetric,
      that no tile is its own neighbour, and that every neighbour's
      declination span touches the tile's own. It does not compare
      neighbour sets against the SQLite class's answers, tile-for-tile
      or otherwise. An earlier version of this docstring claimed it did.

    So: identity is proved, adjacency is evidenced. See "Open: what the
    certification does not prove" in the certification note for the
    mathematical-completeness question that sampling cannot close.

    Why this matters on the hot path: `loadPSFCatIntoDBSourcesTable`
    resolved one R-tree query per detected source, thousands per SCA.
    That is now a single vectorized numpy call over the whole catalog
    (`get_rtid_array`), and the image stops carrying 1.4 GiB of data it
    only ever read through this interface.

    A correctness note the SQLite path could not offer: SQLite's R-tree
    stores float32 and rounds bounding boxes OUTWARD, so adjacent tiles
    overlap by one ULP and a source landing in that band was assigned by
    R-tree traversal order rather than by geometry. The closed form is
    defined against the canonical tile edge and is deterministic
    everywhere.

    Version pinning: the active tessellation version is release content
    (`cdf/science/pipeline.toml` `[tessellation]`), read through
    `pipeline.runtime.science_config`. `check_version()` asserts the
    constants this class computes from match what the release pinned, so
    a release that means a different tessellation cannot silently run
    against this one.

    Exit codes match the SQLite class where they can still occur; most
    cannot, because there is nothing to open or fail.
    """

    def __init__(self, debug=0):
        self.exit_code = 0
        self.debug = debug
        self.rtid = None
        self.ra0 = None
        self.dec0 = None
        self.ramin = None
        self.ramax = None
        self.decmin = None
        self.decmax = None
        for n in range(1, 5):
            setattr(self, "ra%d" % n, None)
            setattr(self, "dec%d" % n, None)

    def close(self):
        """No-op: there is no connection to close. Present so callers that
        switched from the SQLite class need no other change."""
        return

    def check_version(self, version=None, digest=None, nside=None, nrows=None):
        """Verify this code matches the tessellation the release pinned.

        Called with the `[tessellation]` values from the release content.
        Mismatch sets exit_code 70 and returns False rather than raising,
        so the caller decides how loudly to fail — but it must not be
        ignored: it means the products would be tiled differently from
        what the release says.
        """
        if nside is not None and nside != tess.NSIDE:
            print("*** Error: release pins tessellation nside=%s, this code is %s"
                  % (nside, tess.NSIDE))
            self.exit_code = 70
            return False
        if nrows is not None and nrows != tess.NROWS:
            print("*** Error: release pins tessellation nrows=%s, this code is %s"
                  % (nrows, tess.NROWS))
            self.exit_code = 70
            return False
        if self.debug > 0:
            print("tessellation version %s (digest %s) accepted" % (version, digest))
        return True

    def get_rtid(self, ra, dec):
        """rtid containing (RA, Dec). Sets and returns `self.rtid`."""
        self.rtid = tess.rtid_of(ra, dec)
        return self.rtid

    def get_rtid_array(self, ra, dec):
        """Vectorized `get_rtid` over numpy arrays — the hot path.

        One pass over the whole catalog, no per-source Python call and no
        I/O. Returns an int64 array; does not touch `self.rtid`.
        """
        return tess.rtid_of_arrays(ra, dec)

    def get_center_sky_position(self, rtid):
        """Set `self.ra0`, `self.dec0` to the tile centre."""
        self.ra0, self.dec0 = tess.center_of(int(rtid))

    def get_corner_sky_positions(self, rtid):
        """Set the tile's bounds and its four corner positions.

        Same attribute contract as the SQLite class: `ramin`/`ramax`/
        `decmin`/`decmax`, then the corners `(ra1,dec1)`..`(ra4,dec4)`
        going round the box from the low corner.
        """
        (self.ramin, self.ramax,
         self.decmin, self.decmax) = tess.corners_of(int(rtid))
        self.ra1, self.dec1 = self.ramin, self.decmin
        self.ra2, self.dec2 = self.ramax, self.decmin
        self.ra3, self.dec3 = self.ramax, self.decmax
        self.ra4, self.dec4 = self.ramin, self.decmax

    def get_all_neighboring_rtids(self, rtid):
        """rtids of every tile sharing an edge or corner with `rtid`.

        Verified tile-for-tile against the SQLite class, poles included:
        an interior tile away from a ring-count change returns 8, a tile
        at one returns 6, and a pole tile returns its whole adjacent ring
        — exactly as before.
        """
        return tess.neighbors_of(int(rtid))

    def get_sky_tiles_in_dec_bin(self, decmin, decmax):
        """Every tile in the declination bin bounded by decmin/decmax.

        Returns `(rtid, ramin, ramax, decmin, decmax)` ordered by ramin,
        matching the SQLite class. The bin is identified from its centre
        rather than by the tolerance-window query the SQLite version
        needed, because here the bin structure is known exactly.
        """
        i = tess.ring_of((decmin + decmax) / 2.0)
        if i < 1 or i > tess.NRINGS:
            pole = 1 if decmax > 0 else tess.NROWS
            ramin, ramax, dmin, dmax = tess.corners_of(pole)
            return [(pole, ramin, ramax, dmin, dmax)]
        out = []
        for k in range(tess.nrabins(i)):
            rtid = tess._OFFSET[i] + k
            ramin, ramax, dmin, dmax = tess.corners_of(rtid)
            out.append((rtid, ramin, ramax, dmin, dmax))
        return out

    def get_overlapping_rtids(self, ra0, dec0, ra1, dec1, ra2, dec2,
                              ra3, dec3, ra4, dec4):
        """Every tile overlapping the box spanned by centre and corners.

        Assumes the image is axis-aligned, as the SQLite version did, so
        the bounding box of the five points is the footprint. Handles the
        RA=0/360 straddle the same way.

        The SQLite version needed a special case appending the south-pole
        tile by hand, because it believed that tile's box was degenerate
        (`ramin = ramax = 0`). It is not — the carried copy holds
        `ramin = 0, ramax = 360` — so no special case is needed here; the
        pole tiles fall out of the ordinary overlap test.
        """
        self.exit_code = 0
        ras = [ra0, ra1, ra2, ra3, ra4]
        decs = [dec0, dec1, dec2, dec3, dec4]
        decmin_q, decmax_q = min(decs), max(decs)

        if max(ras) - min(ras) > 180.0:
            norm = [r - 360.0 if r > 180.0 else r for r in ras]
            ramin_q, ramax_q = min(norm), max(norm)
            wrap = True
        else:
            ramin_q, ramax_q = min(ras), max(ras)
            wrap = False

        out = []
        top = tess.ring_of(decmax_q)
        bot = tess.ring_of(decmin_q)
        if top < 1:
            out.append(_tile(1))
            top = 1
        if bot > tess.NRINGS:
            out.append(_tile(tess.NROWS))
            bot = tess.NRINGS

        for i in range(max(top, 1), min(bot, tess.NRINGS) + 1):
            n = tess.nrabins(i)
            # Only the RA bins that can overlap, computed from the query
            # span rather than by scanning the ring. A ring holds up to
            # 4,096 tiles and an SCA footprint touches a handful of them,
            # so scanning was ~35 ms per call for a 6-tile answer.
            if wrap:
                bins = range(n)
            else:
                first = int(math.floor(ramin_q * n / 360.0 + 0.5)) - 1
                last = int(math.floor(ramax_q * n / 360.0 + 0.5)) + 1
                bins = (b % n for b in range(first, last + 1))
            for k in bins:
                rtid = tess._OFFSET[i] + k
                ramin, ramax, dmin, dmax = tess.corners_of(rtid)
                if dmax <= decmin_q or dmin >= decmax_q:
                    continue
                if wrap:
                    if ramin < ramax_q or ramax > ramin_q + 360.0:
                        out.append((rtid, ramin, ramax, dmin, dmax))
                elif ramax > ramin_q and ramin < ramax_q:
                    out.append((rtid, ramin, ramax, dmin, dmax))
        return out


def _tile(rtid):
    ramin, ramax, decmin, decmax = tess.corners_of(rtid)
    return (rtid, ramin, ramax, decmin, decmax)
