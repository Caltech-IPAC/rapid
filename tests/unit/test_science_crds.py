"""``rapidpipe.science.crds``: CRDS's best reference by JSON-RPC, with ``urlopen`` replaced.

Contracts: the request is ``get_best_references`` for the context, the WFI
detector and start time, and the one reference type; the name CRDS gives is
returned; a request that gets no answer is made once more by default; when
none answers, a server error, or CRDS answering a reason and not a name, is
``CRDSError``.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from rapidpipe.science import crds

SERVER = "https://crds.example"
ASKED = (SERVER, "roman-operational", "WFI16", "2026-10-24T05:06:07", "area")


@pytest.fixture()
def server(monkeypatch):
    """Queue replies for ``urlopen``; an exception in the queue is raised. Returns (replies, requests)."""
    replies, requests = [], []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(crds.urllib.request, "urlopen", urlopen)
    return replies, requests


def test_best_reference_asks_for_the_detector_and_start_time_and_returns_the_name(server):
    replies, requests = server
    replies.append({"result": {"area": "roman_wfi_area_0085.asdf"}, "error": None, "id": 1})
    assert crds.best_reference(*ASKED) == "roman_wfi_area_0085.asdf"
    (request, timeout), = requests
    assert request.full_url == SERVER + "/json/" and timeout == crds.TIMEOUT_S
    body = json.loads(request.data)
    assert body["method"] == "get_best_references"
    assert body["params"] == ["roman-operational",
                              {"ROMAN.META.INSTRUMENT.NAME": "WFI",
                               "ROMAN.META.INSTRUMENT.DETECTOR": "WFI16",
                               "ROMAN.META.EXPOSURE.START_TIME": "2026-10-24T05:06:07"},
                              ["area"]]


def test_a_request_without_an_answer_is_made_once_more(server):
    replies, requests = server
    replies += [urllib.error.URLError("no route"), {"result": {"area": "roman_wfi_area_0088.asdf"}}]
    assert crds.best_reference(*ASKED) == "roman_wfi_area_0088.asdf"
    assert len(requests) == 2


def test_no_answer_after_the_retries_is_a_crds_error(server):
    replies, requests = server
    replies += [TimeoutError("timed out"), urllib.error.URLError("no route")]
    with pytest.raises(crds.CRDSError, match="URLError"):
        crds.best_reference(*ASKED)
    assert len(requests) == 2


def test_retries_zero_asks_once(server):
    replies, requests = server
    replies.append(TimeoutError("timed out"))
    with pytest.raises(crds.CRDSError, match="TimeoutError"):
        crds.best_reference(*ASKED, retries=0)
    assert len(requests) == 1


def test_a_server_error_is_a_crds_error(server):
    replies, _ = server
    replies += [{"result": None, "error": {"message": "unknown context"}}] * 2
    with pytest.raises(crds.CRDSError, match="unknown context"):
        crds.best_reference(*ASKED)


def test_a_reason_in_place_of_a_name_is_a_crds_error_and_is_not_asked_again(server):
    replies, requests = server
    replies.append({"result": {"area": "NOT FOUND No match found."}})
    with pytest.raises(crds.CRDSError, match="no area reference for WFI16"):
        crds.best_reference(*ASKED)
    assert len(requests) == 1


def test_a_reply_that_is_not_json_is_a_crds_error(server, monkeypatch):
    monkeypatch.setattr(crds.urllib.request, "urlopen", lambda request, timeout: io.BytesIO(b"<html>"))
    with pytest.raises(crds.CRDSError, match="get_best_references"):
        crds.best_reference(*ASKED, retries=0)
