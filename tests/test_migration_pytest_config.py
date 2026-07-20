from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent

import pytest

from testenix.migration_models import MigrationDiagnostic, SourceFile
from testenix.migration_pytest_config import pytest_asyncio_config_diagnostics
from testenix.migration_service import MigrationOptions, MigrationStatus, migrate


def _project(tmp_path: Path, files: Mapping[str, str]) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for relative, content in files.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(dedent(content).lstrip(), encoding="utf-8")
    return root


def _source(root: Path, relative: str = "tests/test_async.py") -> SourceFile:
    path = root / relative
    payload = path.read_bytes()
    return SourceFile(
        path=path,
        project_relative=Path(relative),
        migration_relative=Path(Path(relative).name),
        sha256=hashlib.sha256(payload).hexdigest(),
        text=payload.decode("utf-8"),
    )


def _diagnostics(
    root: Path,
    *,
    pytest_major: int = 9,
    environ: Mapping[str, str] | None = None,
) -> tuple[MigrationDiagnostic, ...]:
    return pytest_asyncio_config_diagnostics(
        project_root=root,
        source_paths=(root / "tests",),
        files=(_source(root),),
        environ={} if environ is None else environ,
        pytest_major=pytest_major,
    )


_BARE_ASYNC_TEST = """
    import asyncio
    import pytest

    @pytest.mark.asyncio
    async def test_poll_once() -> None:
        await asyncio.sleep(0)
"""


def test_solanabot_like_bare_marker_uses_safe_defaults_without_config(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, {"tests/test_async.py": _BARE_ASYNC_TEST})

    assert _diagnostics(root) == ()


def test_sync_only_inventory_ignores_unrelated_asyncio_configuration(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": "def test_sync():\n    assert True\n",
            "pytest.ini": "[pytest]\nasyncio_default_test_loop_scope = session\n",
        },
    )

    assert _diagnostics(root) == ()


def test_pytest_ini_precedes_pyproject_in_the_same_directory(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "[pytest]\nasyncio_default_test_loop_scope = module\n",
            "pyproject.toml": """
                [tool.pytest.ini_options]
                asyncio_default_test_loop_scope = "function"
            """,
        },
    )

    diagnostics = _diagnostics(root, pytest_major=8)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT508_ASYNCIO_CONFIG"]
    assert diagnostics[0].source == "pytest.ini"
    assert "'module'" in diagnostics[0].message


def test_tox_ini_precedes_setup_cfg_in_the_same_directory(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "tox.ini": "[pytest]\nasyncio_default_test_loop_scope = function\n",
            "setup.cfg": "[tool:pytest]\nasyncio_debug = true\n",
        },
    )

    assert _diagnostics(root, pytest_major=8) == ()


def test_nested_config_is_ignored_when_baseline_runs_from_project_root(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "tests/setup.cfg": ("[tool:pytest]\nasyncio_default_test_loop_scope = package\n"),
            "pytest.ini": "[pytest]\nasyncio_default_test_loop_scope = function\n",
        },
    )

    assert _diagnostics(root, pytest_major=8) == ()


def test_empty_pytest_ini_masks_later_pyproject_configuration(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "",
            "pyproject.toml": """
                [tool.pytest.ini_options]
                asyncio_default_test_loop_scope = "session"
            """,
        },
    )

    assert _diagnostics(root, pytest_major=8) == ()


def test_pytest_nine_toml_precedence_is_not_applied_to_pytest_eight(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.toml": "[pytest]\nasyncio_debug = true\n",
            "pytest.ini": "[pytest]\nasyncio_debug = false\n",
        },
    )

    pytest_nine = _diagnostics(root, pytest_major=9)

    assert [diagnostic.code for diagnostic in pytest_nine] == ["PYT508_ASYNCIO_CONFIG"]
    assert pytest_nine[0].source == "pytest.toml"
    assert _diagnostics(root, pytest_major=8) == ()


def test_alias_marker_is_included_in_async_inventory(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": """
                import pytest as pt

                @pt.mark.asyncio
                async def test_alias() -> None:
                    pass
            """,
            "pyproject.toml": """
                [tool.pytest.ini_options]
                asyncio_debug = true
            """,
        },
    )

    diagnostics = _diagnostics(root, pytest_major=9)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT508_ASYNCIO_CONFIG"]


def test_malformed_selected_configuration_fails_closed(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "this is not an ini section\n",
        },
    )

    diagnostics = _diagnostics(root)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT509_PYTEST_CONFIG"]
    assert diagnostics[0].source == "pytest.ini"


def test_invalid_selected_value_reports_its_config_file(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "[pytest]\nasyncio_default_test_loop_scope = process\n",
        },
    )

    diagnostics = _diagnostics(root)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT509_PYTEST_CONFIG"]
    assert diagnostics[0].source == "pytest.ini"


def test_ini_addopts_enabling_asyncio_debug_fails_closed(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "[pytest]\naddopts = -q --asyncio-debug\n",
        },
    )

    diagnostics = _diagnostics(root)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT508_ASYNCIO_CONFIG"]
    assert diagnostics[0].source == "pytest.ini"
    assert "pytest.ini addopts --asyncio-debug" in diagnostics[0].message


def test_native_toml_addopts_changing_loop_scope_fails_closed(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.toml": """
                [pytest]
                addopts = ["-q", "-o", "asyncio_default_test_loop_scope=module"]
            """,
        },
    )

    diagnostics = _diagnostics(root, pytest_major=9)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT508_ASYNCIO_CONFIG"]
    assert diagnostics[0].source == "pytest.toml"
    assert "pytest.toml addopts" in diagnostics[0].message


def test_environment_override_takes_precedence_over_config_addopts(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": ("[pytest]\naddopts = -o asyncio_default_test_loop_scope=session\n"),
        },
    )

    diagnostics = _diagnostics(
        root,
        environ={"PYTEST_ADDOPTS": "-o asyncio_default_test_loop_scope=function"},
    )

    assert diagnostics == ()


def test_outside_environment_path_matches_real_pytest_config_discovery(
    tmp_path: Path,
) -> None:
    from _pytest.config.findpaths import determine_setup

    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "[pytest]\nasyncio_default_test_loop_scope = function\n",
        },
    )
    outside = tmp_path / "empty"
    outside.mkdir()
    parent_config = tmp_path / "pytest.ini"
    parent_config.write_text(
        "[pytest]\nasyncio_default_test_loop_scope = session\n",
        encoding="utf-8",
    )

    real_setup = determine_setup(
        inifile=None,
        override_ini=None,
        args=[str(outside), str(root / "tests")],
        rootdir_cmd_arg=None,
        invocation_dir=root,
    )
    diagnostics = _diagnostics(root, environ={"PYTEST_ADDOPTS": "../empty"})

    assert real_setup[1] == parent_config
    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT509_PYTEST_CONFIG"]
    assert diagnostics[0].source == "<PYTEST_ADDOPTS>"
    assert "outside the project root" in diagnostics[0].message


def test_in_root_nodeid_and_normal_option_values_do_not_false_positive(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, {"tests/test_async.py": _BARE_ASYNC_TEST})
    outside_basetemp = tmp_path / "pytest-temp"
    outside_basetemp.mkdir()

    diagnostics = _diagnostics(
        root,
        environ={
            "PYTEST_ADDOPTS": (
                "-k 'not slow' --tb short --maxfail 1 "
                "--basetemp ../pytest-temp tests/test_async.py::test_poll_once"
            )
        },
    )

    assert diagnostics == ()


@pytest.mark.parametrize("addopts", ["-o", "--override-ini malformed", "-c other.ini"])
def test_malformed_or_explicit_config_addopts_reports_selected_config(
    tmp_path: Path,
    addopts: str,
) -> None:
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": f"[pytest]\naddopts = {addopts}\n",
        },
    )

    diagnostics = _diagnostics(root)

    assert [diagnostic.code for diagnostic in diagnostics] == ["PYT509_PYTEST_CONFIG"]
    assert diagnostics[0].source == "pytest.ini"


@pytest.mark.parametrize(
    ("addopts", "expected_code"),
    [
        ("-q --tb=short", None),
        ("-o asyncio_default_test_loop_scope=function -o asyncio_debug=false", None),
        ("-o asyncio_default_test_loop_scope=class", "PYT508_ASYNCIO_CONFIG"),
        ("-o asyncio_debug=true", "PYT508_ASYNCIO_CONFIG"),
        ("--asyncio-debug", "PYT508_ASYNCIO_CONFIG"),
        ("-c alternate.ini", "PYT509_PYTEST_CONFIG"),
        ("-o", "PYT509_PYTEST_CONFIG"),
        ("--override-ini malformed", "PYT509_PYTEST_CONFIG"),
    ],
)
def test_relevant_pytest_addopts_overrides_fail_closed(
    tmp_path: Path,
    addopts: str,
    expected_code: str | None,
) -> None:
    root = _project(tmp_path, {"tests/test_async.py": _BARE_ASYNC_TEST})

    diagnostics = _diagnostics(root, environ={"PYTEST_ADDOPTS": addopts})

    assert ([diagnostic.code for diagnostic in diagnostics] or [None]) == [expected_code]


def test_migration_service_applies_guard_before_shadow_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    root = _project(
        tmp_path,
        {
            "tests/test_async.py": _BARE_ASYNC_TEST,
            "pytest.ini": "[pytest]\nasyncio_default_test_loop_scope = session\n",
        },
    )

    report = migrate(
        MigrationOptions(
            framework="pytest",
            sources=(Path("tests"),),
            output=Path("converted"),
            workers=2,
            dry_run=True,
            project_root=root,
        )
    )

    assert report.status is MigrationStatus.UNSUPPORTED
    assert report.baseline is None
    assert "PYT508_ASYNCIO_CONFIG" in {diagnostic.code for diagnostic in report.diagnostics}
