"""``rapidpipe.foundation.local.instance_type``: the EC2 instance type from IMDSv2.

Contracts: a token ``PUT`` then a ``GET`` of the instance type with that
token gives the type; an unreachable metadata service gives ``None``.
"""

from __future__ import annotations

import io
import urllib.request

from rapidpipe.foundation import local


def test_the_instance_type_is_read_with_a_session_token(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.get_method(), request.full_url.rsplit("/latest/", 1)[1],
                     request.headers, timeout))
        return io.BytesIO(b"token-1" if request.get_method() == "PUT" else b"m6a.4xlarge")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert local.instance_type() == "m6a.4xlarge"
    assert [(method, path) for method, path, _, _ in seen] == [
        ("PUT", "api/token"), ("GET", "meta-data/instance-type")]
    assert seen[1][2]["X-aws-ec2-metadata-token"] == "token-1"
    assert {timeout for *_, timeout in seen} == {1.0}


def test_an_unreachable_metadata_service_gives_none(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert local.instance_type() is None
