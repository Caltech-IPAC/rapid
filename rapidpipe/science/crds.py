# ported-from: none
"""CRDS's best reference for a Roman WFI detector and date, asked of the server by JSON-RPC.

The intake stage uses it to learn the name of a file's pixel area map, the
``area`` reference, which flight L2 files do not name (``meta.ref_file.area``
is ``INCOMPLETE``). Only names are asked for here; a reference file is fetched
by its name elsewhere.

TODO(crds-area-keyword): if CRDS fills ``meta.ref_file.area`` in flight L2
files, intake reads the name from the file and no longer needs this query;
this module then serves only ``scripts/compute_wfi_zeropoints.py``, which
holds its own copy of the call today and should use this one.
"""

from __future__ import annotations

import json
import urllib.request

#: Seconds one request may take before it is given up.
TIMEOUT_S = 10.0


class CRDSError(RuntimeError):
    """CRDS gave no usable answer: no reply, an error, or no reference of the type asked for."""


def call(server: str, method: str, params: list, timeout: float = TIMEOUT_S):
    """Call one CRDS JSON-RPC method and return its result.

    Parameters
    ----------
    server : str
        CRDS server URL.
    method : str
        JSON-RPC method name, e.g. ``get_best_references``.
    params : list
        The method's positional parameters.
    timeout : float
        Seconds before the request is given up.

    Returns
    -------
    object
        The call's ``result``.

    Raises
    ------
    CRDSError
        If the server does not answer, answers something that is not
        JSON-RPC, or returns an error.
    """
    body = json.dumps({"jsonrpc": "1.0", "method": method, "params": params, "id": 1}).encode()
    request = urllib.request.Request(server.rstrip("/") + "/json/", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            answer = json.load(reply)
    except (OSError, ValueError) as error:          # no connection, a timeout, an HTTP error, not JSON
        raise CRDSError(f"CRDS {method}: {type(error).__name__}: {error}") from error
    if not isinstance(answer, dict) or answer.get("error") or "result" not in answer:
        raise CRDSError(f"CRDS {method}: {answer.get('error') if isinstance(answer, dict) else answer!r}")
    return answer["result"]


def best_reference(server: str, context: str, detector: str, start_time: str, reftype: str, *,
                   timeout: float = TIMEOUT_S, retries: int = 1) -> str:
    """The name of the reference CRDS recommends for one WFI detector and exposure start.

    Parameters
    ----------
    server : str
        CRDS server URL.
    context : str
        CRDS context, e.g. ``roman-operational`` or ``roman_0078.pmap``.
    detector : str
        CRDS detector name, ``WFI01`` to ``WFI18``.
    start_time : str
        Exposure start time [UTC], ``YYYY-MM-DDTHH:MM:SS``.
    reftype : str
        CRDS reference type, e.g. ``area``.
    timeout : float
        Seconds before one request is given up.
    retries : int
        Requests made again after one that got no usable answer.

    Returns
    -------
    str
        The reference file's name, e.g. ``roman_wfi_area_0085.asdf``.

    Raises
    ------
    CRDSError
        If no request got an answer, or CRDS has no reference of this type
        for the detector and date (it answers with a reason, not a name).
    """
    header = {"ROMAN.META.INSTRUMENT.NAME": "WFI",
              "ROMAN.META.INSTRUMENT.DETECTOR": detector,
              "ROMAN.META.EXPOSURE.START_TIME": start_time}
    error: CRDSError | None = None
    for _ in range(1 + max(0, retries)):
        try:
            result = call(server, "get_best_references", [context, header, [reftype]], timeout)
        except CRDSError as failed:
            error = failed
            continue
        name = result.get(reftype) if isinstance(result, dict) else None
        if not isinstance(name, str) or not name.endswith(".asdf"):
            raise CRDSError(f"CRDS has no {reftype} reference for {detector} at {start_time}: {name!r}")
        return name
    raise error
