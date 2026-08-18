"""Fail-fast startup validation + config parse + app wiring (spec: artifacts-startup.md).

When MUSIC3_ARTIFACTS_DIR is set, create_app must abort naming the RESOLVED path if the root is missing,
not a directory, or read-only — so a bad host config fails at startup, not at the first generation. When
unset, persistence is off and the 55 S2 tests keep building the app with an empty environment.
"""

import os

import pytest

from app.main import config_from_env, create_app
from jobs.artifacts import validate_artifacts_root


def test_config_parses_artifacts_dir(tmp_path) -> None:
    assert config_from_env({}).artifacts_dir is None
    got = config_from_env({"MUSIC3_ARTIFACTS_DIR": str(tmp_path)}).artifacts_dir
    assert got == str(tmp_path)


def test_validate_missing_root_raises_naming_resolved_path(tmp_path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(ValueError) as excinfo:
        validate_artifacts_root(str(missing))
    assert os.path.realpath(str(missing)) in str(excinfo.value)


def test_validate_regular_file_raises(tmp_path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        validate_artifacts_root(str(afile))


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses directory permissions")
def test_validate_readonly_root_raises_naming_resolved_path(tmp_path) -> None:
    readonly = tmp_path / "ro"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    try:
        with pytest.raises(ValueError) as excinfo:
            validate_artifacts_root(str(readonly))
        assert os.path.realpath(str(readonly)) in str(excinfo.value)
    finally:
        os.chmod(readonly, 0o700)  # restore so tmp cleanup can remove it


def test_validate_writability_failure_is_deterministic_under_any_uid(tmp_path, monkeypatch) -> None:
    # TEST-1: the chmod-based read-only test above is skipped under root (common in CI containers), so the
    # read-only half of deterministic check 6 would have no coverage there. Simulate the probe failing with
    # EROFS so the "not writable" branch is exercised regardless of uid.
    def _erofs(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("tempfile.mkstemp", _erofs)
    with pytest.raises(ValueError) as excinfo:
        validate_artifacts_root(str(tmp_path))
    assert "not writable" in str(excinfo.value)
    assert os.path.realpath(str(tmp_path)) in str(excinfo.value)


def test_probe_does_not_truncate_a_planted_symlink(tmp_path) -> None:
    # SEC-2: a hostile writer plants a symlink at the OLD predictable probe name pointing at a target
    # file. Startup must prove writability without following/truncating it (the unique mkstemp probe
    # cannot coincide with the planted name).
    target = tmp_path / "target.txt"
    target.write_text("precious")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(target, root / ".artifacts_write_probe")
    assert validate_artifacts_root(str(root)) == os.path.realpath(str(root))
    assert target.read_text() == "precious"   # target untouched


def test_valid_writable_root_returns_resolved(tmp_path) -> None:
    assert validate_artifacts_root(str(tmp_path)) == os.path.realpath(str(tmp_path))


def test_create_app_unset_dir_builds_and_skips_validation() -> None:
    app = create_app(config=config_from_env({}))
    assert app.state.config.artifacts_dir is None


def test_create_app_aborts_on_missing_dir_naming_path(tmp_path) -> None:
    missing = tmp_path / "absent"
    with pytest.raises(ValueError) as excinfo:
        create_app(config=config_from_env({"MUSIC3_ARTIFACTS_DIR": str(missing)}))
    assert os.path.realpath(str(missing)) in str(excinfo.value)


def test_create_app_valid_dir_wires_router_and_runner(tmp_path) -> None:
    app = create_app(config=config_from_env({"MUSIC3_ARTIFACTS_DIR": str(tmp_path)}))
    paths = app.openapi()["paths"]   # the authoritative public surface (what check 4 diffs)
    assert "/artifacts" in paths
    assert "/artifacts/{job_id}/{name}" in paths
