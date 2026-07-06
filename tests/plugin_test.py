from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from pytest_container_structure_test.plugin import BINARY_ENV_VAR

if TYPE_CHECKING:
    from pathlib import Path

STRUCTURE_YAML = """\
schemaVersion: 2.0.0
commandTests:
  - name: python-version
    command: python
    args: ["--version"]
fileExistenceTests:
  - name: app-dir
    path: /app
    shouldExist: true
metadataTest:
  envVars:
    - key: FOO
      value: bar
"""

REPORT_ALL_PASS: dict[str, object] = {
    "Pass": 3,
    "Fail": 0,
    "Total": 3,
    "Results": [
        {"Name": "Command Test: python-version", "Pass": True},
        {"Name": "File Existence Test: app-dir", "Pass": True},
        {"Name": "Metadata Test", "Pass": True},
    ],
}

REPORT_ONE_FAIL: dict[str, object] = {
    "Pass": 2,
    "Fail": 1,
    "Total": 3,
    "Results": [
        {"Name": "Command Test: python-version", "Pass": True},
        {
            "Name": "File Existence Test: app-dir",
            "Pass": False,
            "Errors": ["Expected file /app to exist but it does not"],
        },
        {"Name": "Metadata Test", "Pass": True},
    ],
}

FAKE_BINARY_SOURCE = """\
#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if "CST_FAKE_ARGS" in os.environ:
    with open(os.environ["CST_FAKE_ARGS"], "a") as f:
        f.write(json.dumps(args) + "\\n")
report = os.environ.get("CST_FAKE_REPORT")
if report and "--test-report" in args:
    with open(report) as src, open(args[args.index("--test-report") + 1], "w") as dst:
        dst.write(src.read())
sys.stderr.write(os.environ.get("CST_FAKE_STDERR", ""))
sys.exit(int(os.environ.get("CST_FAKE_EXIT", "0")))
"""


MINI_YAML = 'schemaVersion: 2.0.0\ncommandTests:\n  - name: smoke\n    command: "true"\n'

MINI_REPORT: dict[str, object] = {
    "Pass": 1,
    "Fail": 0,
    "Total": 1,
    "Results": [{"Name": "Command Test: smoke", "Pass": True}],
}


def invocations(args_file: Path) -> list[list[str]]:
    """Read the argv of every fake-binary invocation, one JSON document per line."""
    return [json.loads(line) for line in args_file.read_text(encoding="utf-8").splitlines()]


def arg_value(args: list[str], flag: str) -> str:
    """Return the value following `flag` in a captured argv."""
    return args[args.index(flag) + 1]


def make_project(
    pytester: pytest.Pytester,
    image: str = "fake/image:latest",
    yaml_text: str = STRUCTURE_YAML,
) -> None:
    pytester.makeini(f"[pytest]\ncontainer_structure_tests =\n    structure.yaml={image}\n")
    pytester.makefile(".yaml", structure=yaml_text)


@pytest.fixture
def fake_binary(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a fake container-structure-test binary; returns the captured-args file."""
    script = pytester.path / "fake-container-structure-test.py"
    script.write_text(FAKE_BINARY_SOURCE, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv(BINARY_ENV_VAR, str(script))
    args_file = pytester.path / "captured-args.json"
    monkeypatch.setenv("CST_FAKE_ARGS", str(args_file))
    return args_file


def set_report(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, report: dict[str, object]
) -> None:
    path = pytester.path / "fake-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setenv("CST_FAKE_REPORT", str(path))


def test_collects_individual_tests(pytester: pytest.Pytester) -> None:
    make_project(pytester)
    result = pytester.runpytest("--collect-only", "-q")
    result.stdout.fnmatch_lines(
        [
            "structure.yaml::command:python-version",
            "structure.yaml::file-existence:app-dir",
            "structure.yaml::metadata",
        ]
    )


@pytest.mark.usefixtures("fake_binary")
def test_pass_and_fail_mapping(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(pytester)
    set_report(pytester, monkeypatch, REPORT_ONE_FAIL)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*command:python-version PASSED*",
            "*file-existence:app-dir FAILED*",
            "*Expected file /app to exist but it does not*",
        ]
    )


@pytest.mark.usefixtures("fake_binary")
def test_runs_alongside_python_tests(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(pytester)
    set_report(pytester, monkeypatch, REPORT_ALL_PASS)
    pytester.makepyfile(test_sample="def test_ok() -> None:\n    assert True\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=4)


def test_env_var_default_used_when_unset(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    monkeypatch.delenv("CST_TAG", raising=False)
    make_project(pytester, image="fake/image:${CST_TAG:-latest}")
    set_report(pytester, monkeypatch, REPORT_ALL_PASS)
    result = pytester.runpytest()
    result.assert_outcomes(passed=3)
    (args,) = invocations(fake_binary)
    assert arg_value(args, "--image") == "fake/image:latest"


def test_env_var_expanded_when_set(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    monkeypatch.setenv("CST_TAG", "1.2.3")
    make_project(pytester, image="fake/image:${CST_TAG:-latest}")
    set_report(pytester, monkeypatch, REPORT_ALL_PASS)
    result = pytester.runpytest()
    result.assert_outcomes(passed=3)
    (args,) = invocations(fake_binary)
    assert arg_value(args, "--image") == "fake/image:1.2.3"


def test_unset_env_var_without_default_is_collection_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CST_MISSING_IMAGE", raising=False)
    make_project(pytester, image="${CST_MISSING_IMAGE}")
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*CST_MISSING_IMAGE*is not set*"])


def test_invalid_mapping_entry_is_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\ncontainer_structure_tests =\n    structure.yaml\n")
    pytester.makefile(".yaml", structure=STRUCTURE_YAML)
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*expected '<path/to/config.yaml>=<image>'*"])


@pytest.mark.usefixtures("fake_binary")
def test_binary_failure_fails_every_item(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(pytester)
    monkeypatch.setenv("CST_FAKE_EXIT", "1")
    monkeypatch.setenv("CST_FAKE_STDERR", "Cannot connect to the Docker daemon")
    result = pytester.runpytest()
    result.assert_outcomes(failed=3)
    result.stdout.fnmatch_lines(["*Cannot connect to the Docker daemon*"])


@pytest.mark.usefixtures("fake_binary")
def test_duplicate_test_names_map_by_position(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_text = (
        "schemaVersion: 2.0.0\n"
        "commandTests:\n"
        "  - name: dup\n"
        '    command: "true"\n'
        "  - name: dup\n"
        '    command: "false"\n'
    )
    make_project(pytester, yaml_text=yaml_text)
    report: dict[str, object] = {
        "Pass": 1,
        "Fail": 1,
        "Total": 2,
        "Results": [
            {"Name": "Command Test: dup", "Pass": True},
            {"Name": "Command Test: dup", "Pass": False, "Errors": ["boom"]},
        ],
    }
    set_report(pytester, monkeypatch, report)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*command:dup PASSED*",
            "*command:dup?1? FAILED*",
        ]
    )


@pytest.mark.usefixtures("fake_binary")
def test_report_with_pull_noise_prefix_still_parses(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --pull, the binary writes docker pull progress before the JSON report.

    The noise may itself contain JSON (e.g. registry errors) — the parser must
    skip decoy objects and anchor on the actual report.
    """
    make_project(pytester, yaml_text=MINI_YAML)
    noise = 'latest: Pulling from library/alpine\n{"error": "denied"}\nDigest: sha256:28bd5fe8\n'
    path = pytester.path / "fake-report.json"
    path.write_text(noise + json.dumps(MINI_REPORT), encoding="utf-8")
    monkeypatch.setenv("CST_FAKE_REPORT", str(path))
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_missing_binary_fails_with_clear_message(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    monkeypatch.setenv(BINARY_ENV_VAR, str(pytester.path / "no-such-binary"))
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*failed to run container-structure-test binary*"])


def test_binary_not_on_path_fails_with_clear_message(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    monkeypatch.delenv(BINARY_ENV_VAR, raising=False)
    monkeypatch.setenv("PATH", str(pytester.path))
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*not found on PATH*PYTEST_CONTAINER_STRUCTURE_TEST_BINARY*"])


def test_image_expanding_to_empty_is_collection_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CST_EMPTY", raising=False)
    make_project(pytester, image="${CST_EMPTY:-}", yaml_text=MINI_YAML)
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*image*expanded to an empty string*"])


def test_metadata_expanding_to_empty_is_collection_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CST_EMPTY", raising=False)
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        'metadata = "${CST_EMPTY:-}"\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*metadata*expanded to an empty string*"])


@pytest.mark.usefixtures("fake_binary")
def test_variants_differing_only_in_pull_get_labels(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        "\n"
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        "pull = true\n"
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--collect-only", "-q")
    result.stdout.fnmatch_lines(
        [
            "structure.yaml::command:smoke",
            "structure.yaml::command:smoke?pull?",
        ]
    )


def test_non_mapping_entry_is_collection_error(pytester: pytest.Pytester) -> None:
    yaml_text = "schemaVersion: 2.0.0\ncommandTests:\n  - just-a-string\n"
    make_project(pytester, yaml_text=yaml_text)
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*entries in 'commandTests' must be mappings*"])


@pytest.mark.usefixtures("fake_binary")
def test_report_with_bare_names_still_maps(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a test errors before running, the binary reports the bare test name."""
    make_project(pytester, yaml_text=MINI_YAML)
    report: dict[str, object] = {
        "Pass": 0,
        "Fail": 1,
        "Total": 1,
        "Results": [{"Name": "smoke", "Pass": False, "Errors": ["container creation failed"]}],
    }
    set_report(pytester, monkeypatch, report)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*container creation failed*"])


def test_missing_schema_version_is_collection_error(pytester: pytest.Pytester) -> None:
    yaml_text = 'commandTests:\n  - name: smoke\n    command: "true"\n'
    make_project(pytester, yaml_text=yaml_text)
    result = pytester.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*missing 'schemaVersion'*"])


@pytest.mark.usefixtures("fake_binary")
def test_nameless_error_result_surfaces_run_error(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nameless errored result means the run failed before any test executed."""
    make_project(pytester, yaml_text=MINI_YAML)
    report: dict[str, object] = {
        "Pass": 0,
        "Fail": 1,
        "Total": 1,
        "Results": [
            {"Name": "", "Pass": False, "Errors": ["error parsing config file: bad schema"]}
        ],
    }
    set_report(pytester, monkeypatch, report)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*error parsing config file: bad schema*"])


def test_suite_platform_matrix(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        'platforms = ["linux/amd64", "linux/arm64"]\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*command:smoke?linux/amd64? PASSED*",
            "*command:smoke?linux/arm64? PASSED*",
        ]
    )
    platforms = [arg_value(args, "--platform") for args in invocations(fake_binary)]
    assert sorted(platforms) == ["linux/amd64", "linux/arm64"]


def test_suite_images_and_configs_cross_product(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'configs = ["a.yaml", "b.yaml"]\n'
        'images = ["fake/one:1", "fake/two:2"]\n'
    )
    pytester.makefile(".yaml", a=MINI_YAML, b=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    collected = pytester.runpytest("--collect-only", "-q")
    collected.stdout.fnmatch_lines(
        [
            "a.yaml::command:smoke?fake/one:1?",
            "a.yaml::command:smoke?fake/two:2?",
            "b.yaml::command:smoke?fake/one:1?",
            "b.yaml::command:smoke?fake/two:2?",
        ]
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=4)
    images = [arg_value(args, "--image") for args in invocations(fake_binary)]
    assert sorted(images) == ["fake/one:1", "fake/one:1", "fake/two:2", "fake/two:2"]


def test_suite_flags_land_in_command(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        "pull = true\n"
        'driver = "docker"\n'
        'metadata = "meta.json"\n'
        'extra_args = ["--save"]\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    pytester.makefile(".json", meta="{}")
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    (args,) = invocations(fake_binary)
    assert "--pull" in args
    assert "--save" in args
    assert arg_value(args, "--driver") == "docker"
    assert arg_value(args, "--metadata").endswith("meta.json")


def test_suite_env_expansion_in_image(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    monkeypatch.delenv("CST_SUITE_TAG", raising=False)
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:${CST_SUITE_TAG:-v9}"\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    (args,) = invocations(fake_binary)
    assert arg_value(args, "--image") == "fake/image:v9"


def test_suite_reserved_flag_rejected(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        'extra_args = ["--output=junit"]\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*extra_args may not include '--output'*"])


def test_suite_unknown_key_rejected(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'imagee = "fake/image:latest"\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*unknown key(s): imagee*"])


def test_legacy_ini_and_suites_coexist(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    pytester.makeini("[pytest]\ncontainer_structure_tests =\n    legacy.yaml=fake/legacy:1\n")
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "suite.yaml"\n'
        'image = "fake/suite:1"\n'
    )
    pytester.makefile(".yaml", legacy=MINI_YAML, suite=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--collect-only", "-q")
    result.stdout.fnmatch_lines(
        [
            "legacy.yaml::command:smoke",
            "suite.yaml::command:smoke",
        ]
    )
    run = pytester.runpytest()
    run.assert_outcomes(passed=2)
    images = [arg_value(args, "--image") for args in invocations(fake_binary)]
    assert sorted(images) == ["fake/legacy:1", "fake/suite:1"]


def test_cst_pull_flag_forces_pull(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--cst-pull")
    result.assert_outcomes(passed=1)
    (args,) = invocations(fake_binary)
    assert "--pull" in args


def test_cst_no_pull_flag_strips_pull(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        "pull = true\n"
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--cst-no-pull")
    result.assert_outcomes(passed=1)
    (args,) = invocations(fake_binary)
    assert "--pull" not in args


def test_cst_pull_flags_are_mutually_exclusive(pytester: pytest.Pytester) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    result = pytester.runpytest("--cst-pull", "--cst-no-pull")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--cst-pull and --cst-no-pull are mutually exclusive*"])


def test_cst_platform_flag_collapses_matrix(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    pytester.makepyprojecttoml(
        "[[tool.pytest-container-structure-test.suites]]\n"
        'config = "structure.yaml"\n'
        'image = "fake/image:latest"\n'
        'platforms = ["linux/amd64", "linux/arm64"]\n'
    )
    pytester.makefile(".yaml", structure=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--cst-platform", "linux/amd64", "-v")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*command:smoke PASSED*"])
    (args,) = invocations(fake_binary)
    assert arg_value(args, "--platform") == "linux/amd64"


def test_cst_platform_flag_injects_into_platformless_mapping(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--cst-platform", "linux/arm64")
    result.assert_outcomes(passed=1)
    (args,) = invocations(fake_binary)
    assert arg_value(args, "--platform") == "linux/arm64"


def test_cst_platform_flag_repeatable(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    fake_binary: Path,
) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest(
        "--cst-platform", "linux/amd64", "--cst-platform", "linux/arm64", "-v"
    )
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*command:smoke?linux/amd64? PASSED*",
            "*command:smoke?linux/arm64? PASSED*",
        ]
    )
    platforms = [arg_value(args, "--platform") for args in invocations(fake_binary)]
    assert sorted(platforms) == ["linux/amd64", "linux/arm64"]


@pytest.mark.usefixtures("fake_binary")
def test_debug_logging_shows_command(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(pytester, yaml_text=MINI_YAML)
    set_report(pytester, monkeypatch, MINI_REPORT)
    result = pytester.runpytest("--log-cli-level=DEBUG")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*running: *test --image fake/image:latest --config *"])


def test_integration_with_docker(pytester: pytest.Pytester) -> None:
    if shutil.which("container-structure-test") is None:
        pytest.skip("container-structure-test binary not on PATH")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not available")
    if subprocess.run([docker, "info"], capture_output=True, check=False).returncode != 0:  # noqa: S603
        pytest.skip("docker daemon not running")
    pull = subprocess.run(  # noqa: S603
        [docker, "pull", "alpine:latest"], capture_output=True, check=False
    )
    if pull.returncode != 0:
        pytest.skip("could not pull alpine image")
    yaml_text = (
        "schemaVersion: 2.0.0\n"
        "commandTests:\n"
        "  - name: os-release\n"
        "    command: cat\n"
        '    args: ["/etc/alpine-release"]\n'
        "fileExistenceTests:\n"
        "  - name: sh-exists\n"
        "    path: /bin/sh\n"
        "    shouldExist: true\n"
    )
    make_project(pytester, image="alpine:latest", yaml_text=yaml_text)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)
