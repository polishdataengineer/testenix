from __future__ import annotations

import copy
import json
import os
import textwrap
from pathlib import Path

import pytest

import testenix.runner as runner_module
from testenix.cli import main
from testenix.config import TestenixConfig
from testenix.contracts import Scope, Status
from testenix.discovery import discover, discover_selected
from testenix.runner import collect_trusted_manifest, run
from testenix.sharding import (
    CollectionManifestError,
    ShardingPolicy,
    TrustedCollectionManifest,
    assess_collection_sharding,
    build_trusted_collection_manifest,
    deserialize_trusted_collection_manifest,
    serialize_trusted_collection_manifest,
    trusted_collection_manifest_to_dict,
    verify_trusted_collection_manifest,
)


def _suite(directory: Path, source: str, *, name: str = "test_sample.py") -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _config(workers: int) -> TestenixConfig:
    return TestenixConfig(workers=workers, retries=0, history_path=None)


def test_sharding_policy_requires_an_explicit_boolean() -> None:
    assert ShardingPolicy().intra_module is False
    assert ShardingPolicy(intra_module=True).intra_module is True
    with pytest.raises(TypeError, match="boolean"):
        ShardingPolicy(intra_module=1)  # type: ignore[arg-type]


def test_trusted_manifest_round_trips_as_deterministic_portable_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _suite(
        tmp_path / "tests",
        """
        from testenix import case, cases

        @cases(case(id="one", value={"answer": 42}))
        def test_value(value):
            assert value["answer"] == 42
        """,
    )
    collection = discover("tests")

    manifest = build_trusted_collection_manifest("tests", collection)
    encoded = serialize_trusted_collection_manifest(manifest)
    restored = deserialize_trusted_collection_manifest(encoded)

    assert restored == manifest
    assert serialize_trusted_collection_manifest(restored) == encoded
    assert restored.collection_roots == ("tests",)
    assert restored.files[0].path == "tests/test_sample.py"
    assert restored.tests[0].parameters == {"value": "<redacted>"}
    assert len(restored.files[0].sha256) == 64
    assert verify_trusted_collection_manifest(restored, "tests")


def test_manifest_redacts_dynamic_parameter_secrets_and_still_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = "testenix-secret-must-not-reach-manifest"
    monkeypatch.setenv("TESTENIX_CASE_SECRET", secret)
    _suite(
        tmp_path / "tests",
        """
        import os

        from testenix import case

        @case(token=os.environ["TESTENIX_CASE_SECRET"])
        def test_secret(token):
            assert token == os.environ["TESTENIX_CASE_SECRET"]
        """,
    )
    collection = discover("tests")
    assert collection.items[0].spec.parameters == {"token": secret}

    encoded = serialize_trusted_collection_manifest(
        build_trusted_collection_manifest("tests", collection)
    )
    manifest = deserialize_trusted_collection_manifest(encoded)
    result = run("tests", _config(1), trusted_manifest=manifest)

    assert secret not in encoded
    assert manifest.tests[0].parameters == {"token": "<redacted>"}
    assert result.tests[0].test.parameters == {"token": "<redacted>"}
    assert result.tests[0].status is Status.PASS


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.__setitem__("schema_version", 999), "unsupported"),
        (
            lambda data: data["files"][0].__setitem__("path", "../escape.py"),
            "safe relative",
        ),
        (
            lambda data: data["files"].append(copy.deepcopy(data["files"][0])),
            "duplicate source",
        ),
        (
            lambda data: data["tests"].append(copy.deepcopy(data["tests"][0])),
            "duplicate test",
        ),
        (
            lambda data: data["sharding"].append(copy.deepcopy(data["sharding"][0])),
            "duplicate sharding",
        ),
    ],
)
def test_trusted_manifest_rejects_bad_schema_traversal_and_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    _suite(tmp_path / "tests", "def test_ok():\n    assert True\n")
    manifest = build_trusted_collection_manifest("tests", discover("tests"))
    data = trusted_collection_manifest_to_dict(manifest)

    assert callable(mutation)
    mutation(data)
    with pytest.raises(CollectionManifestError, match=message):
        deserialize_trusted_collection_manifest(data)


def test_trusted_manifest_rejects_duplicate_json_keys() -> None:
    with pytest.raises(CollectionManifestError, match="duplicate JSON object key"):
        deserialize_trusted_collection_manifest('{"format":"a","format":"b"}')


def test_manifest_verification_fails_closed_for_changed_added_and_removed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    original = _suite(tests, "def test_one():\n    assert True\n", name="test_one.py")
    manifest = build_trusted_collection_manifest("tests", discover("tests"))

    original.write_text("def test_one():\n    assert False\n", encoding="utf-8")
    assert not verify_trusted_collection_manifest(manifest, "tests")

    original.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    added = _suite(tests, "def test_two():\n    assert True\n", name="test_two.py")
    assert not verify_trusted_collection_manifest(manifest, "tests")

    added.unlink()
    original.unlink()
    assert not verify_trusted_collection_manifest(manifest, "tests")


def test_execution_worker_rechecks_manifest_digest_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = _suite(tmp_path / "tests", "def test_one():\n    assert True\n")
    manifest = build_trusted_collection_manifest("tests", discover("tests"))
    imported_changed_source = tmp_path / "changed-source-imported"
    verify = runner_module.verify_trusted_collection_manifest

    def verify_then_replace(
        candidate: TrustedCollectionManifest,
        paths: tuple[str, ...],
    ) -> bool:
        verified = verify(candidate, paths)
        assert verified
        source.write_text(
            textwrap.dedent(
                f"""
                from pathlib import Path

                Path({str(imported_changed_source)!r}).write_text("unsafe", encoding="utf-8")

                def test_one():
                    assert True
                """
            ),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(runner_module, "verify_trusted_collection_manifest", verify_then_replace)

    result = run("tests", _config(1), trusted_manifest=manifest)

    assert not imported_changed_source.exists()
    assert result.tests[0].status is Status.INFRA_ERROR
    assert all(attempt.status is Status.INFRA_ERROR for attempt in result.tests[0].attempts)
    assert any(
        "source digest mismatch" in (phase.message or "")
        for attempt in result.tests[0].attempts
        for phase in attempt.phases
    )


def test_manifest_fingerprints_imported_case_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    helper = _suite(
        tests,
        """
        from testenix import case

        def generated_cases():
            return (case(id="one", value=1),)
        """,
        name="case_helper.py",
    )
    _suite(
        tests,
        """
        from case_helper import generated_cases
        from testenix import cases

        @cases(*generated_cases())
        def test_value(value):
            assert value > 0
        """,
    )
    manifest = build_trusted_collection_manifest("tests", discover("tests"))

    assert verify_trusted_collection_manifest(manifest, "tests")
    assert any(fingerprint.path == "tests/case_helper.py" for fingerprint in manifest.files)

    helper.write_text(
        textwrap.dedent(
            """
            from testenix import case

            def generated_cases():
                return (
                    case(id="one", value=1),
                    case(id="two", value=2),
                )
            """
        ),
        encoding="utf-8",
    )

    assert not verify_trusted_collection_manifest(manifest, "tests")

    result = run("tests", _config(1), trusted_manifest=manifest)

    assert len(result.tests) == 2
    assert {test.status for test in result.tests} == {Status.PASS}


def test_manifest_fingerprints_collection_import_nested_inside_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    nested = _suite(
        tests,
        """
        from testenix import case

        def build_cases():
            return (case(id="one", value=1),)
        """,
        name="nested_case_helper.py",
    )
    _suite(
        tests,
        """
        def generated_cases():
            from nested_case_helper import build_cases

            return build_cases()
        """,
        name="case_helper.py",
    )
    _suite(
        tests,
        """
        from case_helper import generated_cases
        from testenix import cases

        @cases(*generated_cases())
        def test_value(value):
            assert value > 0
        """,
    )

    manifest = build_trusted_collection_manifest("tests", discover("tests"))

    assert any(fingerprint.path == "tests/nested_case_helper.py" for fingerprint in manifest.files)
    nested.write_text(
        "from testenix import case\n\ndef build_cases(): return (case(id='two', value=2),)\n",
        encoding="utf-8",
    )
    assert not verify_trusted_collection_manifest(manifest, "tests")


def test_execution_worker_rechecks_imported_case_generator_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    helper = _suite(
        tests,
        """
        from testenix import case

        def generated_cases():
            return (case(id="one", value=1),)
        """,
        name="case_helper.py",
    )
    _suite(
        tests,
        """
        from case_helper import generated_cases
        from testenix import cases

        @cases(*generated_cases())
        def test_value(value):
            assert value == 1
        """,
    )
    manifest = build_trusted_collection_manifest("tests", discover("tests"))
    imported_changed_helper = tmp_path / "changed-helper-imported"
    verify = runner_module.verify_trusted_collection_manifest

    def verify_then_replace(
        candidate: TrustedCollectionManifest,
        paths: tuple[str, ...],
    ) -> bool:
        verified = verify(candidate, paths)
        assert verified
        helper.write_text(
            textwrap.dedent(
                f"""
                from pathlib import Path

                from testenix import case

                Path({str(imported_changed_helper)!r}).write_text("unsafe", encoding="utf-8")

                def generated_cases():
                    return (case(id="one", value=1),)
                """
            ),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(runner_module, "verify_trusted_collection_manifest", verify_then_replace)

    result = run("tests", _config(1), trusted_manifest=manifest)

    assert not imported_changed_helper.exists()
    assert result.tests[0].status is Status.INFRA_ERROR
    assert any(
        "case_helper.py" in (phase.message or "")
        and "source digest mismatch" in (phase.message or "")
        for attempt in result.tests[0].attempts
        for phase in attempt.phases
    )


def test_configured_trusted_manifest_skips_the_full_collection_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    counter = tmp_path / "imports.txt"
    path = _suite(
        tmp_path / "tests",
        f"""
        from pathlib import Path

        with Path({str(counter)!r}).open("a", encoding="utf-8") as output:
            output.write("imported\\n")

        def test_ok():
            assert True
        """,
    )
    collection = discover("tests")
    manifest = build_trusted_collection_manifest("tests", collection)
    manifest_path = tmp_path / "collection.json"
    manifest_path.write_text(serialize_trusted_collection_manifest(manifest), encoding="utf-8")
    assert counter.read_text(encoding="utf-8").splitlines() == ["imported"]

    result = run(
        "tests",
        TestenixConfig(workers=1, history_path=None, manifest_path=manifest_path),
    )

    assert [test.status for test in result.tests] == [Status.PASS]
    # One producer import + one execution-worker import.  A normal run would
    # also import the whole suite in its isolated collection worker.
    assert counter.read_text(encoding="utf-8").splitlines() == ["imported", "imported"]
    assert result.tests[0].test.path == path.relative_to(tmp_path).as_posix()


def test_stale_trusted_manifest_falls_back_to_isolated_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _suite(tmp_path / "tests", "def test_one():\n    assert True\n")
    manifest = build_trusted_collection_manifest("tests", discover("tests"))
    path.write_text(
        "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )

    result = run("tests", _config(1), trusted_manifest=manifest)

    assert len(result.tests) == 2
    assert {test.status for test in result.tests} == {Status.PASS}


def test_programmatic_manifest_path_rejects_malformed_json(tmp_path: Path) -> None:
    path = _suite(tmp_path, "def test_ok(): pass\n")
    manifest_path = tmp_path / "broken.json"
    manifest_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(CollectionManifestError, match="invalid collection manifest JSON"):
        run(
            str(path),
            TestenixConfig(workers=1, history_path=None, manifest_path=manifest_path),
        )


def test_selected_execution_supports_decorators_that_change_function_name(
    tmp_path: Path,
) -> None:
    path = _suite(
        tmp_path,
        """
        def rename(function):
            def wrapped():
                function()
            return wrapped

        @rename
        def test_original():
            assert True
        """,
    )

    result = run(str(path), _config(1))

    assert len(result.tests) == 1
    assert result.tests[0].test.function_name == "wrapped"
    assert result.tests[0].status is Status.PASS


def test_default_module_affinity_remains_unchanged(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        def test_one(): pass
        def test_two(): pass
        def test_three(): pass
        def test_four(): pass
        def test_five(): pass
        def test_six(): pass
        """,
    )

    result = run(str(path), _config(3))

    assert {test.status for test in result.tests} == {Status.PASS}
    assert len({test.attempts[0].worker_id for test in result.tests}) == 1
    assert result.workers_used == 1


def test_opt_in_shards_an_eligible_module_across_workers(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        def test_one(): pass
        def test_two(): pass
        def test_three(): pass
        def test_four(): pass
        def test_five(): pass
        def test_six(): pass
        """,
    )

    result = run(
        str(path),
        _config(3),
        sharding_policy=ShardingPolicy(intra_module=True),
    )

    assert {test.status for test in result.tests} == {Status.PASS}
    assert len({test.attempts[0].worker_id for test in result.tests}) > 1
    assert result.workers_used == 3


def test_workers_used_reports_executed_non_empty_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _suite(
        tmp_path,
        "\n".join(f"def test_{index}(): pass" for index in range(6)),
    )
    original_schedule = runner_module.schedule_lpt

    def collapse_plan_to_one_shard(*args, **kwargs):  # type: ignore[no-untyped-def]
        return original_schedule(args[0], 1, *args[2:], **kwargs)

    monkeypatch.setattr(runner_module, "schedule_lpt", collapse_plan_to_one_shard)

    result = runner_module.run(
        str(path),
        _config(3),
        sharding_policy=ShardingPolicy(intra_module=True),
    )

    assert result.workers_used == 1
    assert len({test.attempts[0].worker_id for test in result.tests}) == 1


def test_auto_workers_follow_real_units_before_and_after_opt_in_sharding(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        "\n".join(f"def test_{index}(): pass" for index in range(12)),
    )
    config = TestenixConfig(workers="auto", history_path=None)

    affinity = run(str(path), config)
    sharded = run(
        str(path),
        TestenixConfig(workers="auto", history_path=None, shard_modules=True),
    )

    assert affinity.workers_used == 1
    assert sharded.workers_used == min(4, os.cpu_count() or 1, 12)
    assert sharded.shardable_paths == (path.as_posix(),)


def test_function_autouse_fixture_does_not_block_opt_in_sharding(tmp_path: Path) -> None:
    path = _suite(
        tmp_path,
        """
        from testenix import fixture

        @fixture(scope="test", autouse=True)
        def isolated_setup(tmp_path):
            assert tmp_path.is_dir()

        def test_one(): pass
        def test_two(): pass
        def test_three(): pass
        def test_four(): pass
        """,
    )

    collection = discover(str(path))
    assert collection.fixtures[0].scope is Scope.TEST
    assert assess_collection_sharding(collection)[0].eligible

    result = run(
        str(path),
        _config(2),
        sharding_policy=ShardingPolicy(intra_module=True),
    )
    assert len({test.attempts[0].worker_id for test in result.tests}) == 2


def test_imported_local_fixture_scope_fails_closed_across_manifest_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    scope_source = _suite(tests, 'SCOPE = "test"\n', name="fixture_config.py")
    _suite(
        tests,
        """
        from fixture_config import SCOPE
        from testenix import fixture

        @fixture(scope=SCOPE)
        def shared():
            return 42

        def test_one(shared):
            assert shared == 42

        def test_two(shared):
            assert shared == 42
        """,
    )
    manifest = build_trusted_collection_manifest("tests", discover("tests"))

    assert len(manifest.sharding) == 1
    assert not manifest.sharding[0].eligible
    assert any(
        "statically guaranteed test scope" in blocker for blocker in manifest.sharding[0].blockers
    )
    assert any(fingerprint.path == "tests/fixture_config.py" for fingerprint in manifest.files)

    scope_source.write_text('SCOPE = "session"\n', encoding="utf-8")
    assert not verify_trusted_collection_manifest(manifest, "tests")

    result = run(
        "tests",
        TestenixConfig(workers=2, history_path=None, shard_modules=True),
        trusted_manifest=manifest,
    )

    assert {test.status for test in result.tests} == {Status.PASS}
    assert result.workers_used == 1
    assert result.shardable_paths == ()


def test_imported_fixture_provider_fails_closed_across_manifest_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    provider = _suite(
        tests,
        """
        from testenix import fixture

        @fixture
        def shared():
            return 42
        """,
        name="external_fixture_provider.py",
    )
    _suite(
        tests,
        """
        from external_fixture_provider import shared

        def test_one(shared):
            assert shared == 42

        def test_two(shared):
            assert shared == 42
        """,
    )
    manifest = build_trusted_collection_manifest("tests", discover("tests"))

    assert len(manifest.sharding) == 1
    assert not manifest.sharding[0].eligible
    assert any(
        "outside the collected module source" in blocker
        for blocker in manifest.sharding[0].blockers
    )
    assert any(
        fingerprint.path == "tests/external_fixture_provider.py" for fingerprint in manifest.files
    )

    provider.write_text(
        textwrap.dedent(
            """
            from testenix import fixture

            @fixture(scope="session")
            def shared():
                return 42
            """
        ),
        encoding="utf-8",
    )
    # Local imports that can influence collection are fingerprinted alongside
    # selected test files, so the stale manifest is rejected before scheduling.
    assert not verify_trusted_collection_manifest(manifest, "tests")

    result = run(
        "tests",
        TestenixConfig(workers=2, history_path=None, shard_modules=True),
        trusted_manifest=manifest,
    )

    assert {test.status for test in result.tests} == {Status.PASS}
    assert result.workers_used == 1
    assert result.shardable_paths == ()


@pytest.mark.parametrize("scope", ["module", "session"])
def test_wide_fixture_scope_falls_back_to_module_affinity(tmp_path: Path, scope: str) -> None:
    path = _suite(
        tmp_path,
        f"""
        from testenix import fixture

        @fixture(scope={scope!r})
        def shared():
            return 42

        def test_one(shared): assert shared == 42
        def test_two(shared): assert shared == 42
        def test_three(shared): assert shared == 42
        """,
    )

    result = run(
        str(path),
        _config(3),
        sharding_policy=ShardingPolicy(intra_module=True),
    )

    assert {test.status for test in result.tests} == {Status.PASS}
    assert len({test.attempts[0].worker_id for test in result.tests}) == 1


def test_obvious_mutable_global_and_import_lifecycle_are_conservative_blockers(
    tmp_path: Path,
) -> None:
    path = _suite(
        tmp_path,
        """
        events = []
        print("import lifecycle")

        def test_one():
            events.append("one")
        """,
    )

    decision = assess_collection_sharding(discover(str(path)))[0]

    assert not decision.eligible
    assert any("module-level collection" in blocker for blocker in decision.blockers)
    assert any("import-time call" in blocker for blocker in decision.blockers)


@pytest.mark.parametrize(
    ("source", "expected_blocker"),
    [
        (
            """
            STATE = ([],)

            def test_one():
                STATE[0].append("changed")
            """,
            "module-level collection 'STATE'",
        ),
        (
            """
            class State:
                values = []

            def test_one():
                State.values.append("changed")
            """,
            "mutable class state on 'State'",
        ),
        (
            """
            class State:
                pass

            State.values = []

            def test_one():
                State.values.append("changed")
            """,
            "mutable class state on 'State'",
        ),
        (
            """
            class Outer:
                class Inner:
                    values = []

            def test_one():
                Outer.Inner.values.append("changed")
            """,
            "mutable class state on 'Outer'",
        ),
    ],
)
def test_nested_and_class_mutable_state_block_module_sharding(
    tmp_path: Path,
    source: str,
    expected_blocker: str,
) -> None:
    path = _suite(tmp_path, source)

    decision = assess_collection_sharding(discover(str(path)))[0]

    assert not decision.eligible
    assert any(expected_blocker in blocker for blocker in decision.blockers)


@pytest.mark.parametrize(
    ("source", "expected_blocker"),
    [
        (
            """
            def connect():
                return object()

            CLIENT = connect()

            def test_one():
                assert CLIENT is not None
            """,
            "assignment call connect",
        ),
        (
            """
            HANDLE: object = open(__file__, encoding="utf-8")
            HANDLE.close()

            def test_one():
                assert HANDLE.closed
            """,
            "assignment call open",
        ),
        (
            """
            def register():
                def decorate(function):
                    return function
                return decorate

            @register()
            def test_one():
                assert True
            """,
            "decorator call register",
        ),
        (
            """
            def register(function):
                function.registered = True
                return function

            @register
            def test_one():
                assert test_one.registered
            """,
            "decorator call register",
        ),
        (
            """
            def factory():
                return 42

            def helper(default=factory()):
                return default

            def test_one():
                assert helper() == 42
            """,
            "default call factory",
        ),
        (
            """
            def connect():
                return int

            def test_one(value: connect() = 1) -> connect():
                assert value == 1
            """,
            "annotation call connect",
        ),
        (
            """
            def connect():
                return int

            VALUE: connect() = 1

            def test_one():
                assert VALUE == 1
            """,
            "annotation call connect",
        ),
        (
            """
            class Base:
                pass

            def factory():
                return Base

            class Derived(factory()):
                pass

            def test_one():
                assert issubclass(Derived, Base)
            """,
            "class base call factory",
        ),
        (
            """
            def connect():
                return None

            class Helper:
                connect()

            def test_one():
                assert Helper is not None
            """,
            "import-time call connect",
        ),
        (
            """
            def validate():
                return True

            class Helper:
                assert validate()

            def test_one():
                assert Helper is not None
            """,
            "assertion call validate",
        ),
        (
            """
            STATE = 0

            def factory():
                return 1

            STATE += factory()

            def test_one():
                assert STATE == 1
            """,
            "assignment call factory",
        ),
        (
            """
            import sys

            ORIGINAL = sys.path[0]

            def register_path():
                return ORIGINAL

            sys.path.insert(0, register_path())
            assert sys.path.pop(0) == ORIGINAL

            def test_one():
                assert True
            """,
            "expression call register_path",
        ),
    ],
)
def test_definition_time_calls_block_safe_module_sharding(
    tmp_path: Path,
    source: str,
    expected_blocker: str,
) -> None:
    path = _suite(tmp_path, source)

    decision = assess_collection_sharding(discover(str(path)))[0]

    assert not decision.eligible
    assert any(expected_blocker in blocker for blocker in decision.blockers)


def test_plain_constants_and_testenix_decorator_factories_remain_shardable(
    tmp_path: Path,
) -> None:
    path = _suite(
        tmp_path,
        """
        from testenix import case, cases, fixture, skip, test, xfail

        CONSTANT = ("plain", 42)

        @fixture(autouse=True)
        def isolated_setup():
            assert CONSTANT[1] == 42

        @test(tags={"unit"}, timeout=1.0)
        @cases(case(id="one", value=1), case(id="two", value=2))
        @skip("not skipped", when=False)
        @xfail("not expected to fail", when=False)
        def test_value(value):
            assert value in {1, 2}

        @test
        def test_bare_decorator():
            assert CONSTANT[0] == "plain"
        """,
    )

    decision = assess_collection_sharding(discover(str(path)))[0]

    assert decision.eligible
    assert decision.blockers == ()


def test_postponed_annotations_do_not_create_false_import_time_blockers(
    tmp_path: Path,
) -> None:
    path = _suite(
        tmp_path,
        """
        from __future__ import annotations

        from testenix import test

        def connect():
            return int

        VALUE: connect() = 1

        @test
        def test_one(value: connect() = VALUE) -> connect():
            assert value == 1
        """,
    )

    decision = assess_collection_sharding(discover(str(path)))[0]

    assert decision.eligible
    assert decision.blockers == ()


def test_worker_crash_recovery_semantics_survive_intra_module_sharding(tmp_path: Path) -> None:
    state = tmp_path / "crash-once"
    path = _suite(
        tmp_path,
        f"""
        import os
        from pathlib import Path

        def test_crashes_once():
            state = Path({str(state)!r})
            if not state.exists():
                state.write_text("crashed", encoding="utf-8")
                os._exit(19)

        def test_stays_green():
            assert True
        """,
    )

    result = run(
        str(path),
        _config(2),
        sharding_policy=ShardingPolicy(intra_module=True),
    )
    by_name = {test.test.function_name: test for test in result.tests}

    assert by_name["test_stays_green"].status is Status.PASS
    recovered = by_name["test_crashes_once"]
    assert recovered.status is Status.FLAKY
    assert [attempt.status for attempt in recovered.attempts] == [Status.CRASH, Status.PASS]


def test_selected_rediscovery_materialises_only_requested_tests_and_all_fixtures(
    tmp_path: Path,
) -> None:
    path = _suite(
        tmp_path,
        """
        from testenix import case, cases, fixture

        @fixture
        def value(): return 42

        def test_selected(value): assert value == 42

        @cases(case(id="one", number=1), case(id="two", number=2))
        def test_unrelated(number): assert number > 0
        """,
    )

    collection = discover_selected(path, {"test_selected"})

    assert [item.function_name for item in collection.tests] == ["test_selected"]
    assert any(fixture.name == "value" for fixture in collection.fixtures)


def test_manifest_json_is_plain_inert_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _suite(tmp_path / "tests", "def test_ok(): pass\n")
    encoded = serialize_trusted_collection_manifest(
        build_trusted_collection_manifest("tests", discover("tests"))
    )

    decoded = json.loads(encoded)
    assert decoded["format"] == "testenix.collection-manifest"
    assert "__reduce__" not in encoded


def test_manifest_cli_uses_isolated_collection_and_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _suite(tmp_path / "tests", "def test_ok(): pass\n")
    output = tmp_path / ".testenix" / "collection.json"

    assert main(["manifest", "tests", "--output", str(output)]) == 0
    manifest = deserialize_trusted_collection_manifest(output.read_bytes())
    assert manifest == collect_trusted_manifest("tests")

    before = output.read_bytes()
    assert main(["manifest", "tests", "--output", str(output)]) == 2
    assert output.read_bytes() == before
    assert "will not be replaced" in capsys.readouterr().err


def test_public_manifest_collection_resolves_paths_from_explicit_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    _suite(
        project_root / "tests",
        """
        def test_from_project_root():
            assert True
        """,
    )
    monkeypatch.chdir(outside)

    manifest = collect_trusted_manifest("tests", project_root=project_root)

    assert manifest.collection_roots == ("tests",)
    assert [fingerprint.path for fingerprint in manifest.files] == ["tests/test_sample.py"]
    assert [test.function_name for test in manifest.tests] == ["test_from_project_root"]
    assert [test.path for test in manifest.tests] == ["tests/test_sample.py"]
