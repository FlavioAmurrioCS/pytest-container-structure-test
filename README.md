# pytest-container-structure-test

[![PyPI - Version](https://img.shields.io/pypi/v/pytest-container-structure-test.svg)](https://pypi.org/project/pytest-container-structure-test)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pytest-container-structure-test.svg)](https://pypi.org/project/pytest-container-structure-test)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/FlavioAmurrioCS/pytest-container-structure-test/main.svg)](https://results.pre-commit.ci/latest/github/FlavioAmurrioCS/pytest-container-structure-test/main)

-----

Run [container-structure-test](https://github.com/GoogleContainerTools/container-structure-test) configs from pytest, with every test in your YAML config reported as an individual pytest test — right alongside your regular Python tests.

```console
$ pytest -v
structure.yaml::command:os-release PASSED                    [ 25%]
structure.yaml::command:gunicorn-installed PASSED            [ 50%]
structure.yaml::file-existence:app-dir PASSED                [ 75%]
test_app.py::test_healthcheck PASSED                         [100%]
```

## Table of Contents

- [Installation](#installation)
- [Usage](#usage)
- [Test matrix: multiple images, configs, and platforms](#test-matrix-multiple-images-configs-and-platforms)
- [How it works](#how-it-works)
- [License](#license)

## Installation

```console
pip install pytest-container-structure-test
```

The plugin invokes the `container-structure-test` binary from your `PATH`. Any install route works:

- [upstream releases / brew](https://github.com/GoogleContainerTools/container-structure-test#installation) (e.g. `brew install container-structure-test`)
- `pip install container-structure-test` — the [PyPI wheel](https://pypi.org/project/container-structure-test/) that ships the binary as a console script in your environment
- or point `PYTEST_CONTAINER_STRUCTURE_TEST_BINARY` at a specific binary

A running Docker daemon is required to execute the tests (not to collect them).

## Usage

Declare your config files and the image each one targets in one place, using the `container_structure_tests` ini option (in `pyproject.toml`, `pytest.ini`, `tox.ini`, or `setup.cfg`). Each entry has the form `<path/to/config.yaml>=<image>`:

```toml
# pyproject.toml
[tool.pytest.ini_options]
container_structure_tests = [
  "tests/structure/web.yaml=myorg/web:${WEB_VERSION:-latest}",
  "tests/structure/db.yaml=${DB_IMAGE}",
  "tests/structure/cli.yaml=myorg/cli:latest",
]
```

Paths are relative to the pytest rootdir and must live under a directory pytest collects (typically `tests/`).

The image value supports environment-variable expansion, so the image name or tag can come from CI:

- `$VAR` or `${VAR}` — expands from the environment; referencing an unset variable is a collection error.
- `${VAR:-default}` — uses `default` when `VAR` is unset or empty, so runs work locally without exports.

Write your config files exactly as `container-structure-test` expects — nothing custom:

```yaml
# tests/structure/web.yaml
schemaVersion: 2.0.0
commandTests:
  - name: gunicorn-installed
    command: gunicorn
    args: ["--version"]
fileExistenceTests:
  - name: app-dir
    path: /app
    shouldExist: true
metadataTest:
  exposedPorts: ["8000"]
```

Then just run pytest. Every entry in `commandTests`, `fileExistenceTests`, `fileContentTests`, and `licenseTests` — plus the `metadataTest` block — becomes its own pytest test with its own pass/fail, and failures include the errors, stdout, and stderr reported by the tool:

```console
$ pytest -v tests/
tests/structure/web.yaml::command:gunicorn-installed PASSED
tests/structure/web.yaml::file-existence:app-dir FAILED
tests/structure/web.yaml::metadata PASSED
tests/test_app.py::test_healthcheck PASSED

=================================== FAILURES ===================================
______________________ structure/web.yaml::file-existence:app-dir _____________
File Existence Test: app-dir: FAIL
error: Expected file /app to exist but it does not
```

## Test matrix: multiple images, configs, and platforms

When one image per config isn't enough — you want the same config against several images or architectures, or you need other `container-structure-test test` flags — declare **suites** in a plugin-owned table in `pyproject.toml`:

```toml
[[tool.pytest-container-structure-test.suites]]
configs   = ["tests/structure/web.yaml"]
image     = "myorg/web:${WEB_VERSION:-latest}"
platforms = ["linux/amd64", "linux/arm64"]
pull      = true

[[tool.pytest-container-structure-test.suites]]
configs    = ["tests/structure/base.yaml", "tests/structure/db.yaml"]
images     = ["${DB_IMAGE}", "myorg/db:edge"]
driver     = "docker"
extra_args = ["--save"]
```

Each suite expands to the cross product **configs × images × platforms**, and every combination is one `container-structure-test` invocation. When a config file runs in more than one combination, the differing dimensions show up as a suffix on each test's node ID:

```console
tests/structure/web.yaml::command:gunicorn-installed[linux/amd64] PASSED
tests/structure/web.yaml::command:gunicorn-installed[linux/arm64] PASSED
tests/structure/db.yaml::command:psql-installed[myorg/db:edge] FAILED
```

Fields per suite:

| Field | Maps to | Notes |
|---|---|---|
| `config` / `configs` | `--config` | one required; paths relative to rootdir |
| `image` / `images` | `--image` | one required; env-var expansion applies |
| `platform` / `platforms` | `--platform` | optional; omitted → host default |
| `pull` | `--pull` | boolean |
| `driver` | `--driver` | e.g. `docker`, `tar`, `host` |
| `metadata` | `--metadata` | path relative to rootdir; env-var expansion applies |
| `extra_args` | passed verbatim | any other flag, e.g. `["--save", "--runtime", "runsc"]` |

Unknown keys are rejected with a clear error (typo protection). `extra_args` may not include the flags the plugin itself manages (`--config`, `--image`, `--platform`, `--output`, `--test-report`, `--no-color`, `--quiet`) — overriding those would break result mapping.

> [!NOTE]
> With the classic Docker image store, a tag holds **one** platform at a time — pulling `linux/amd64` replaces a local `linux/arm64` image under the same tag. When testing multiple platforms, set `pull = true` so each run fetches its own variant, and enable Docker's containerd image store if you want multi-platform tags cached side by side.

The simple `container_structure_tests` ini option keeps working and can be combined with suites; each of its entries is just a suite of one config, one image, and default flags.

### Pipeline overrides

The config declares the full intended matrix; command-line flags adjust it per invocation, so the same config works locally and in CI:

```console
# arch-limited pipeline runner with a fresh image cache:
pytest --cst-platform=linux/amd64 --cst-pull

# local run right after `docker build` — don't let a registry pull clobber the local tag:
pytest --cst-no-pull
```

- `--cst-platform PLATFORM` (repeatable) overrides the platform of **every** configured run, collapsing any declared platform matrix to the given value(s); runs that declared no platform get it injected.
- `--cst-pull` / `--cst-no-pull` force pulling on or off for every run (mutually exclusive; default is whatever each suite configured).

Flags apply to all configured runs. A repo can bake defaults with `addopts` in `[tool.pytest.ini_options]`. To see the exact binary invocations for debugging, run with `--log-cli-level=DEBUG`.

## How it works

- Collection only parses the YAML — `pytest --collect-only` never touches Docker.
- At run time, the binary is invoked **once per config × image × platform combination** and each collected test looks up its own result from that run's JSON report, so N tests in one config cost one image run per combination.
- If the binary itself fails (Docker daemon down, image missing), every test in that config fails with the captured stderr.
- The binary is resolved from `PATH`; set `PYTEST_CONTAINER_STRUCTURE_TEST_BINARY` to use a specific `container-structure-test` binary instead.

## License

`pytest-container-structure-test` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
