"""Path-containment choke point (spec: path-containment.md; INV-8, R-17).

The traversal matrix is refused by ``validate_job_id`` — a pure check that touches no filesystem — so a
crafted id is rejected BEFORE any path is built or opened. ``resolve_within`` is the belt-and-suspenders
that defeats a symlink planted inside the root via ``realpath``, not via file permissions.
"""

import os
import uuid

import pytest

from jobs.artifacts import (
    JobIdError,
    UntrustedPathError,
    is_within,
    resolve_within,
    validate_job_id,
)


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        "../../etc",
        "a/b",
        "/etc/passwd",
        "a\x00b",
        "%2f",          # a URL-encoded separator as a literal id
        "..%2f..",
        "",
        "a" * 129,      # over the length ceiling
        ".",
        "a.b",          # dots are not in the allow-list
        "A_B-c9\n",     # trailing newline must not slip past ($ vs \Z; F3)
        "valid\nid",
    ],
)
def test_traversal_ids_refused_before_open(bad: str) -> None:
    # Pure refusal: validate_job_id does no filesystem work, so raising here is proof of refusal
    # before any path is constructed or any file is opened (adversarial case 1).
    with pytest.raises(JobIdError):
        validate_job_id(bad)


@pytest.mark.parametrize("good", ["abc_123-XYZ", "artifact", uuid.uuid4().hex])
def test_valid_ids_pass(good: str) -> None:
    assert validate_job_id(good) == good


def test_is_within_is_separator_aware() -> None:
    assert is_within("/a/b", "/a") is True
    assert is_within("/a", "/a") is True
    assert is_within("/ab", "/a") is False        # sibling prefix, not containment
    assert is_within("/a-evil/x", "/a") is False


def test_symlink_escape_refused_by_resolution(tmp_path) -> None:
    # A symlink INSIDE the root pointing at /etc/passwd must be refused by resolve_within's realpath,
    # not by luck of file permissions (adversarial case 2).
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("s3cr3t")
    link = root / "evil"
    os.symlink(outside, link)
    with pytest.raises(UntrustedPathError):
        resolve_within(str(link), str(root))


def test_dotdot_candidate_escaping_root_refused(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    escaping = os.path.join(str(root), "..", "outside", "x")
    with pytest.raises(UntrustedPathError):
        resolve_within(escaping, str(root))


def test_sibling_root_is_not_containment(tmp_path) -> None:
    root = tmp_path / "foo"
    root.mkdir()
    sibling = tmp_path / "foo-evil"
    sibling.mkdir()
    with pytest.raises(UntrustedPathError):
        resolve_within(str(sibling / "x"), str(root))


def test_url_scheme_and_nul_refused(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    with pytest.raises(UntrustedPathError):
        resolve_within("http://example.com/x", str(root))
    with pytest.raises(UntrustedPathError):
        resolve_within("a\x00b", str(root))


def test_contained_path_resolves(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    candidate = os.path.join(str(root), "job123", "audio.wav")
    resolved = resolve_within(candidate, str(root))
    assert resolved == os.path.realpath(candidate)
    assert is_within(resolved, os.path.realpath(str(root)))
