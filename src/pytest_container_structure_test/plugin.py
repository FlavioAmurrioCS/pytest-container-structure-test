from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest
import yaml
from typing_extensions import override

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator
    from pathlib import Path

    from _pytest._code.code import TerminalRepr
    from _pytest._code.code import TracebackStyle

INI_OPTION = "container_structure_tests"
TOOL_TABLE = "pytest-container-structure-test"
BINARY_ENV_VAR = "PYTEST_CONTAINER_STRUCTURE_TEST_BINARY"

logger = logging.getLogger(__name__)

_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("commandTests", "Command Test", "command"),
    ("fileExistenceTests", "File Existence Test", "file-existence"),
    ("fileContentTests", "File Content Test", "file-content"),
)

_ALLOWED_SUITE_KEYS = frozenset(
    {
        "config",
        "configs",
        "image",
        "images",
        "platform",
        "platforms",
        "pull",
        "driver",
        "metadata",
        "extra_args",
    }
)

# Flags the plugin itself relies on for collection and result mapping.
_RESERVED_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "-i",
        "--image",
        "--platform",
        "-o",
        "--output",
        "--test-report",
        "--no-color",
        "-q",
        "--quiet",
    }
)

_ENV_VAR_RE = re.compile(r"\$(?:(\w+)|\{(\w+)(?::-([^}]*))?\})")

# (item_name, acceptable report result names, result_index)
_TestEntry = tuple[str, tuple[str, ...], int]


@dataclass(frozen=True)
class RunSpec:
    """One binary invocation: a structure-test config against one image variant."""

    config_path: Path
    image: str
    platform: str | None = None
    pull: bool = False
    driver: str | None = None
    metadata: str | None = None
    extra_args: tuple[str, ...] = ()


@dataclass
class _ResolvedRun:
    """A RunSpec with environment variables expanded, plus its cached report."""

    spec: RunSpec
    image: str
    metadata: str | None
    extra_args: tuple[str, ...]
    results: dict[str, list[dict[str, object]]] | None = None
    error: str | None = None

    def describe(self) -> str:
        parts = [f"image: {self.image}"]
        if self.spec.platform:
            parts.append(f"platform: {self.spec.platform}")
        return ", ".join(parts)


_mapping_key: pytest.StashKey[dict[Path, list[RunSpec]]] = pytest.StashKey()


class StructureTestRunError(Exception):
    """The container-structure-test binary could not produce a test report."""


class StructureTestFailedError(Exception):
    """An individual container structure test reported a failure."""

    def __init__(self, result: dict[str, object], run: _ResolvedRun) -> None:
        super().__init__(result.get("Name"))
        self.result = result
        self.run = run

    def report(self) -> str:
        lines = [f"{self.result.get('Name')}: FAIL ({self.run.describe()})"]
        errors = self.result.get("Errors")
        if isinstance(errors, list):
            lines.extend(f"error: {error}" for error in cast("list[object]", errors))
        for stream in ("Stdout", "Stderr"):
            content = self.result.get(stream)
            if content:
                lines.append(f"{stream.lower()}:\n{content}")
        return "\n".join(lines)


class UnsetEnvironmentVariableError(Exception):
    def __init__(self, variable: str) -> None:
        super().__init__(variable)
        self.variable = variable


def _expand_env_vars(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        variable = match.group(1) or match.group(2)
        default = match.group(3)
        resolved = os.environ.get(variable, "")
        if resolved:
            return resolved
        if default is not None:
            return default
        if variable in os.environ:
            return ""
        raise UnsetEnvironmentVariableError(variable)

    return _ENV_VAR_RE.sub(replace, value)


def _unmatched_run_errors(results: dict[str, list[dict[str, object]]]) -> list[str]:
    """Errors from nameless results — emitted when the run fails before any test executes.

    Example: a config file the binary cannot parse yields a single result with
    Name "" and Errors ["error parsing config file: ..."].
    """
    errors: list[str] = []
    for result in results.get("", []):
        raw = result.get("Errors")
        if isinstance(raw, list):
            errors.extend(str(error) for error in cast("list[object]", raw))
    return errors


def _parse_report(text: str) -> object:
    """Parse the JSON test report, skipping any noise the binary wrote before it.

    With --pull, container-structure-test writes docker pull progress into the
    same stream as the test report, so the JSON does not always start at offset 0
    and the noise may itself contain JSON (e.g. registry errors). Only accept an
    object that looks like a report.
    """
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value: object = decoder.raw_decode(text, index)[0]
        except ValueError:
            value = None
        if isinstance(value, dict):
            report = cast("dict[str, object]", value)
            if "Results" in report or "Total" in report:
                return report
        index = text.find("{", index + 1)
    msg = "no test report JSON object found"
    raise ValueError(msg)


def _binary_path() -> str:
    configured = os.environ.get(BINARY_ENV_VAR)
    if configured:
        return configured
    binary = shutil.which("container-structure-test")
    if binary is None:
        msg = (
            "container-structure-test binary not found on PATH; install it "
            "(https://github.com/GoogleContainerTools/container-structure-test#installation) "
            f"or set {BINARY_ENV_VAR} to its location"
        )
        raise StructureTestRunError(msg)
    return binary


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        INI_OPTION,
        type="linelist",
        default=[],
        help=(
            "container-structure-test configs to run, one per line, in the form "
            "<path/to/config.yaml>=<image>. Paths are relative to the pytest rootdir. "
            "The image value may reference environment variables with $VAR, ${VAR} or "
            "${VAR:-default}. For multi-image/multi-platform matrices, use the "
            f"[[tool.{TOOL_TABLE}.suites]] table in pyproject.toml instead."
        ),
    )
    group = parser.getgroup("container-structure-test")
    group.addoption(
        "--cst-platform",
        action="append",
        default=None,
        metavar="PLATFORM",
        help=(
            "override the platform of every configured container-structure-test run "
            "(repeatable); collapses any configured platform matrix to the given value(s)"
        ),
    )
    group.addoption(
        "--cst-pull",
        action="store_true",
        help="force a pull of the image before every container-structure-test run",
    )
    group.addoption(
        "--cst-no-pull",
        action="store_true",
        help="never pull images, even for runs configured with pull = true",
    )


def _suite_error(index: int, message: str) -> pytest.UsageError:
    return pytest.UsageError(f"[tool.{TOOL_TABLE}].suites[{index}]: {message}")


def _string_values(
    suite: dict[str, object], single_key: str, plural_key: str, index: int
) -> list[str] | None:
    single = suite.get(single_key)
    plural = suite.get(plural_key)
    if single is not None and plural is not None:
        raise _suite_error(index, f"use either {single_key!r} or {plural_key!r}, not both")
    if single is not None:
        if not isinstance(single, str) or not single:
            raise _suite_error(index, f"{single_key!r} must be a non-empty string")
        return [single]
    if plural is None:
        return None
    if not isinstance(plural, list) or not plural:
        raise _suite_error(index, f"{plural_key!r} must be a non-empty array of strings")
    values: list[str] = []
    for value in cast("list[object]", plural):
        if not isinstance(value, str) or not value:
            raise _suite_error(index, f"{plural_key!r} must be a non-empty array of strings")
        values.append(value)
    return values


def _optional_string(suite: dict[str, object], key: str, index: int) -> str | None:
    value = suite.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _suite_error(index, f"{key!r} must be a non-empty string")
    return value


def _extra_args(suite: dict[str, object], index: int) -> tuple[str, ...]:
    raw = suite.get("extra_args")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _suite_error(index, "'extra_args' must be an array of strings")
    values: list[str] = []
    for arg in cast("list[object]", raw):
        if not isinstance(arg, str):
            raise _suite_error(index, "'extra_args' must be an array of strings")
        flag = arg.split("=", 1)[0]
        if flag in _RESERVED_FLAGS:
            raise _suite_error(
                index, f"extra_args may not include {flag!r} (managed by the plugin)"
            )
        values.append(arg)
    return tuple(values)


def _parse_suite(config: pytest.Config, suite: dict[str, object], index: int) -> list[RunSpec]:
    unknown = set(suite) - _ALLOWED_SUITE_KEYS
    if unknown:
        raise _suite_error(index, f"unknown key(s): {', '.join(sorted(unknown))}")
    configs = _string_values(suite, "config", "configs", index)
    if configs is None:
        raise _suite_error(index, "requires 'config' or 'configs'")
    images = _string_values(suite, "image", "images", index)
    if images is None:
        raise _suite_error(index, "requires 'image' or 'images'")
    platforms = _string_values(suite, "platform", "platforms", index)
    platform_values: list[str | None] = [None] if platforms is None else list(platforms)
    pull = suite.get("pull", False)
    if not isinstance(pull, bool):
        raise _suite_error(index, "'pull' must be a boolean")
    driver = _optional_string(suite, "driver", index)
    metadata = _optional_string(suite, "metadata", index)
    extra_args = _extra_args(suite, index)
    return [
        RunSpec(
            config_path=(config.rootpath / config_value).resolve(),
            image=image,
            platform=platform,
            pull=pull,
            driver=driver,
            metadata=metadata,
            extra_args=extra_args,
        )
        for config_value in configs
        for image in images
        for platform in platform_values
    ]


def _pyproject_path(config: pytest.Config) -> Path | None:
    if config.inipath is not None and config.inipath.name == "pyproject.toml":
        return config.inipath
    candidate = config.rootpath / "pyproject.toml"
    return candidate if candidate.is_file() else None


def _pyproject_suite_specs(config: pytest.Config) -> list[RunSpec]:
    pyproject = _pyproject_path(config)
    if pyproject is None:
        return []
    with open(pyproject, "rb") as file:
        data: dict[str, object] = tomllib.load(file)
    tool = data.get("tool")
    table_obj = cast("dict[str, object]", tool).get(TOOL_TABLE) if isinstance(tool, dict) else None
    if table_obj is None:
        return []
    if not isinstance(table_obj, dict):
        msg = f"[tool.{TOOL_TABLE}]: must be a table"
        raise pytest.UsageError(msg)
    table = cast("dict[str, object]", table_obj)
    unknown = set(table) - {"suites"}
    if unknown:
        msg = f"[tool.{TOOL_TABLE}]: unknown key(s): {', '.join(sorted(unknown))}"
        raise pytest.UsageError(msg)
    suites_obj = table.get("suites")
    if suites_obj is None:
        return []
    if not isinstance(suites_obj, list):
        msg = f"[tool.{TOOL_TABLE}]: 'suites' must be an array of tables"
        raise pytest.UsageError(msg)
    specs: list[RunSpec] = []
    for index, suite_obj in enumerate(cast("list[object]", suites_obj)):
        if not isinstance(suite_obj, dict):
            raise _suite_error(index, "must be a table")
        specs.extend(_parse_suite(config, cast("dict[str, object]", suite_obj), index))
    return specs


def _legacy_specs(config: pytest.Config) -> list[RunSpec]:
    specs: list[RunSpec] = []
    for line in cast("list[str]", config.getini(INI_OPTION)):
        path_part, separator, image_part = line.partition("=")
        path_text = path_part.strip()
        image_text = image_part.strip()
        if not separator or not path_text or not image_text:
            msg = f"{INI_OPTION}: invalid entry {line!r}, expected '<path/to/config.yaml>=<image>'"
            raise pytest.UsageError(msg)
        specs.append(RunSpec(config_path=(config.rootpath / path_text).resolve(), image=image_text))
    return specs


def _apply_cli_overrides(config: pytest.Config, specs: list[RunSpec]) -> list[RunSpec]:
    platforms = cast("list[str] | None", config.getoption("--cst-platform"))
    pull_on = cast("bool", config.getoption("--cst-pull"))
    pull_off = cast("bool", config.getoption("--cst-no-pull"))
    if pull_on and pull_off:
        msg = "--cst-pull and --cst-no-pull are mutually exclusive"
        raise pytest.UsageError(msg)
    overridden: list[RunSpec] = []
    for spec in specs:
        variants = [spec] if platforms is None else [replace(spec, platform=p) for p in platforms]
        if pull_on or pull_off:
            variants = [replace(variant, pull=pull_on) for variant in variants]
        overridden.extend(variants)
    return overridden


def _config_mapping(config: pytest.Config) -> dict[Path, list[RunSpec]]:
    cached = config.stash.get(_mapping_key, None)
    if cached is not None:
        return cached
    mapping: dict[Path, list[RunSpec]] = {}
    specs = _apply_cli_overrides(config, _legacy_specs(config) + _pyproject_suite_specs(config))
    for spec in specs:
        bucket: list[RunSpec] | None = mapping.get(spec.config_path)
        if bucket is None:
            bucket = []
            mapping[spec.config_path] = bucket
        if spec not in bucket:
            bucket.append(spec)
    config.stash[_mapping_key] = mapping
    return mapping


def pytest_configure(config: pytest.Config) -> None:
    _config_mapping(config)


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    specs = _config_mapping(parent.config).get(file_path.resolve())
    if not specs:
        return None
    return ContainerStructureTestFile.from_parent(  # pyright: ignore[reportUnknownMemberType]
        parent, path=file_path, specs=specs
    )


# Printable label part per RunSpec dimension; None means "nothing to show".
_LABEL_DIMENSIONS: tuple[Callable[[_ResolvedRun], str | None], ...] = (
    lambda run: run.image,
    lambda run: run.spec.platform,
    lambda run: f"driver={run.spec.driver}" if run.spec.driver else None,
    lambda run: "pull" if run.spec.pull else None,
    lambda run: f"metadata={os.path.basename(run.metadata)}" if run.metadata else None,
    lambda run: " ".join(run.extra_args) if run.extra_args else None,
)


def _variant_labels(runs: list[_ResolvedRun]) -> list[str]:
    """Node-ID suffixes: every dimension that differs across the file's runs contributes."""
    if len(runs) <= 1:
        return [""] * len(runs)
    varying = [dim for dim in _LABEL_DIMENSIONS if len({dim(run) for run in runs}) > 1]
    labels = ["|".join(part for dim in varying if (part := dim(run)) is not None) for run in runs]
    if len(set(labels)) != len(labels):
        labels = [f"{label}|{index}" if label else str(index) for index, label in enumerate(labels)]
    return labels


class ContainerStructureTestFile(pytest.File):
    def __init__(
        self,
        *,
        specs: list[RunSpec],
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        super().__init__(**kwargs)
        self.specs = specs
        self._runs: list[_ResolvedRun] = []

    @override
    def collect(self) -> Iterator[pytest.Item]:
        document = self._load_document()
        self._runs = [self._resolve_spec(spec) for spec in self.specs]
        labels = _variant_labels(self._runs)
        entries = list(self._iter_test_entries(document))
        for run, label in zip(self._runs, labels, strict=True):
            for base_name, result_names, result_index in entries:
                item_name = f"{base_name}[{label}]" if label else base_name
                yield self._make_item(
                    name=item_name, result_names=result_names, result_index=result_index, run=run
                )

    def _load_document(self) -> dict[str, object]:
        try:
            loaded: object = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            msg = f"invalid YAML in {self.path}: {exc}"
            raise self.CollectError(msg) from exc
        if not isinstance(loaded, dict):
            msg = f"{self.path} does not look like a container-structure-test config"
            raise self.CollectError(msg)
        document = cast("dict[str, object]", loaded)
        if not document.get("schemaVersion"):
            msg = (
                f"{self.path}: missing 'schemaVersion' — container-structure-test "
                "requires it (e.g. schemaVersion: 2.0.0)"
            )
            raise self.CollectError(msg)
        return document

    def _resolve_spec(self, spec: RunSpec) -> _ResolvedRun:
        try:
            image = self._expand_non_empty(spec.image, "image")
            metadata = (
                self._expand_non_empty(spec.metadata, "metadata")
                if spec.metadata is not None
                else None
            )
            extra_args = tuple(_expand_env_vars(arg) for arg in spec.extra_args)
        except UnsetEnvironmentVariableError as exc:
            msg = (
                f"configuration for {self.path.name} references environment variable "
                f"{exc.variable!r} which is not set "
                "(use ${" + exc.variable + ":-default} to provide a fallback)"
            )
            raise self.CollectError(msg) from exc
        if metadata is not None:
            metadata = str((self.config.rootpath / metadata).resolve())
        return _ResolvedRun(spec=spec, image=image, metadata=metadata, extra_args=extra_args)

    def _expand_non_empty(self, value: str, field: str) -> str:
        expanded = _expand_env_vars(value)
        if not expanded:
            msg = f"{self.path.name}: {field} value {value!r} expanded to an empty string"
            raise self.CollectError(msg)
        return expanded

    def _iter_test_entries(self, document: dict[str, object]) -> Iterator[_TestEntry]:
        """Yield (item_name, report_result_names, result_index) per test in the config."""
        yield from self._iter_named_entries(document)
        if document.get("metadataTest"):
            yield ("metadata", ("Metadata Test",), 0)
        license_obj = document.get("licenseTests")
        license_count = (
            len(cast("list[object]", license_obj)) if isinstance(license_obj, list) else 0
        )
        for index in range(license_count):
            name = "license" if license_count == 1 else f"license[{index}]"
            yield (name, ("License Test",), index)

    def _iter_named_entries(self, document: dict[str, object]) -> Iterator[_TestEntry]:
        for section, prefix, short_name in _SECTIONS:
            entries_obj = document.get(section)
            if entries_obj is None:
                continue
            if not isinstance(entries_obj, list):
                msg = f"{self.path}: {section!r} must be a list"
                raise self.CollectError(msg)
            seen: dict[str, int] = {}
            for entry in cast("list[object]", entries_obj):
                if not isinstance(entry, dict):
                    msg = f"{self.path}: entries in {section!r} must be mappings"
                    raise self.CollectError(msg)
                name = cast("dict[str, object]", entry).get("name")
                if not isinstance(name, str) or not name:
                    msg = f"{self.path}: every entry in {section!r} must have a 'name'"
                    raise self.CollectError(msg)
                index = seen.get(name, 0)
                seen[name] = index + 1
                item_name = f"{short_name}:{name}"
                if index:
                    item_name = f"{item_name}[{index}]"
                # The binary names results "<prefix>: <name>" normally, but bare
                # "<name>" when a test errors before running (e.g. container
                # creation failure) — accept both.
                yield (item_name, (f"{prefix}: {name}", name), index)

    def _make_item(
        self, *, name: str, result_names: tuple[str, ...], result_index: int, run: _ResolvedRun
    ) -> ContainerStructureTestItem:
        return ContainerStructureTestItem.from_parent(  # pyright: ignore[reportUnknownMemberType]
            self, name=name, result_names=result_names, result_index=result_index, run=run
        )

    def run_results(self, run: _ResolvedRun) -> dict[str, list[dict[str, object]]]:
        if run.error is not None:
            raise StructureTestRunError(run.error)
        if run.results is None:
            try:
                run.results = self._execute(run)
            except StructureTestRunError as exc:
                run.error = str(exc)
                raise
        return run.results

    def _build_command(self, run: _ResolvedRun, binary: str, report_path: str) -> list[str]:
        command = [
            binary,
            "test",
            "--image",
            run.image,
            "--config",
            str(self.path),
            "--output",
            "json",
            "--no-color",
            "--test-report",
            report_path,
        ]
        if run.spec.platform:
            command += ["--platform", run.spec.platform]
        if run.spec.pull:
            command.append("--pull")
        if run.spec.driver:
            command += ["--driver", run.spec.driver]
        if run.metadata:
            command += ["--metadata", run.metadata]
        command += list(run.extra_args)
        return command

    def _execute(self, run: _ResolvedRun) -> dict[str, list[dict[str, object]]]:
        binary = _binary_path()
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "report.json")
            command = self._build_command(run, binary, report_path)
            logger.debug("running: %s", shlex.join(command))
            try:
                process = subprocess.run(  # noqa: S603
                    command, capture_output=True, text=True, check=False
                )
            except OSError as exc:
                msg = f"failed to run container-structure-test binary {binary!r}: {exc}"
                raise StructureTestRunError(msg) from exc
            logger.debug("container-structure-test exited with %s", process.returncode)
            report_text: str | None = None
            try:
                with open(report_path, encoding="utf-8") as file:
                    report_text = file.read()
                report: object = _parse_report(report_text)
            except (OSError, ValueError) as exc:
                msg = (
                    f"container-structure-test did not produce a test report for "
                    f"{self.path.name} ({run.describe()}, exit code {process.returncode})\n"
                    f"command: {' '.join(command)}\n"
                    f"stdout:\n{process.stdout}\n"
                    f"stderr:\n{process.stderr}"
                )
                if report_text is not None:
                    msg += f"\nreport file contents:\n{report_text[:500]}"
                raise StructureTestRunError(msg) from exc
        results: dict[str, list[dict[str, object]]] = {}
        raw_results = (
            cast("dict[str, object]", report).get("Results") if isinstance(report, dict) else None
        )
        if isinstance(raw_results, list):
            for result_obj in cast("list[object]", raw_results):
                if isinstance(result_obj, dict):
                    result = cast("dict[str, object]", result_obj)
                    results.setdefault(str(result.get("Name")), []).append(result)
        return results


class ContainerStructureTestItem(pytest.Item):
    def __init__(
        self,
        *,
        result_names: tuple[str, ...],
        result_index: int,
        run: _ResolvedRun,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        super().__init__(**kwargs)  # pyright: ignore[reportUnknownMemberType]
        self.result_names = result_names
        self.result_index = result_index
        self.run = run

    @override
    def runtest(self) -> None:
        parent = cast("ContainerStructureTestFile", self.parent)
        results = parent.run_results(self.run)
        matches: list[dict[str, object]] = []
        for candidate in self.result_names:
            matches = results.get(candidate, [])
            if matches:
                break
        if self.result_index >= len(matches):
            run_errors = _unmatched_run_errors(results)
            if run_errors:
                msg = (
                    f"container-structure-test failed for {self.path.name} "
                    f"({self.run.describe()}): " + "; ".join(run_errors)
                )
                raise StructureTestRunError(msg)
            msg = (
                f"no result named {' or '.join(map(repr, self.result_names))} "
                f"(occurrence {self.result_index}) in the container-structure-test report "
                f"({self.run.describe()}); report contained: {sorted(results)}"
            )
            raise StructureTestRunError(msg)
        result = matches[self.result_index]
        if not result.get("Pass", False):
            raise StructureTestFailedError(result, self.run)

    @override
    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: TracebackStyle | None = None,
    ) -> str | TerminalRepr:
        if isinstance(excinfo.value, StructureTestFailedError):
            return excinfo.value.report()
        if isinstance(excinfo.value, StructureTestRunError):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style)

    @override
    def reportinfo(self) -> tuple[Path, int | None, str]:
        return self.path, None, self.name
