# Mutation testing

A passing suite only proves the tests agree with the code. Mutation testing
measures something else: it breaks the code on purpose (flips a `<` to `<=`,
changes `32` to `31`, deletes a `not`) and checks that some test fails. A mutant
that no test notices is a "survivor": logic the suite does not actually protect.

Tool: [cosmic-ray](https://github.com/sixty-north/cosmic-ray) 8.9.0, dev
dependency in `pyproject.toml` (`[dependency-groups] dev`). `mutmut` was not
chosen because its README states that on Windows it only runs inside WSL, and
this repo is developed on Windows.

## How to run

One session per module; configs live in `mutation/<module>.toml`. Each one
points `test-command` at the test files that exercise that module and sets a
60 s timeout per mutant.

```bash
uv sync
uv run cosmic-ray init mutation/registry.toml scratch/registry.sqlite
uv run cosmic-ray baseline mutation/registry.toml     # the tests must pass unmutated
timeout 900 uv run cosmic-ray exec mutation/registry.toml scratch/registry.sqlite
uv run cosmic-ray dump scratch/registry.sqlite > scratch/registry.jsonl
```

Each line of the dump is `[work item, result]`; `test_outcome` is `killed`,
`survived` or `incompetent`. A timeout counts as `killed`
(`output == "timeout"`). Session files (`*.sqlite`) are git-ignored.

**Warning:** cosmic-ray edits `stackhelx/*.py` in place and leaves the mutation
applied if it is interrupted. Before every commit,
`git diff --stat stackhelx/` must be empty. To avoid the risk entirely, run each
session on a copy of the repo (`cp -r stackhelx tests pyproject.toml mutation`
to a scratch folder and put it first on `PYTHONPATH`); that is how the numbers
below were taken. Run the suite as `pytest tests` afterwards: pytest also collects the copies under `scratch/` and fails with "import file mismatch".

Mutants run in random order and each costs one test-command run (3 to 30 s), so
a full pass does not fit in a sitting. The scores below are **partial**: the
mutants executed in a 15 minute cap per module.

## Scope and score (2026-11-26, 505 tests, full suite in 76 s with `-n auto`)

Raw counts include `X | None` annotations mutated into `X + None`. On Python 3.14
annotations are evaluated lazily, so those mutants cannot fail: they are
equivalent. The "adjusted" column drops every `BitOr` mutant (all the ones
that survived were annotations).

| Module | Mutants | Run | Killed | Survived | Incompetent | Timeouts (in killed) | Adjusted killed |
|---|---|---|---|---|---|---|---|
| `ports.py` | 541 | 256 | 63 | 192 | 1 | 0 | 63 of 152 (41%) |
| `detect.py` | 1100 | 131 | 99 | 32 | 0 | 0 | 99 of 109 (91%) |
| `registry.py` | 469 | 352 | 145 | 207 | 0 | 6 | 145 of 282 (51%) |
| `runner.py` | 719 | 19 | 16 | 0 | 3 | 13 | [not representative] |

`runner.py`: each mutant needs about 30 s because its tests start real processes
and wait for healthchecks, so only 19 ran. Of the 16 killed, 13 were timeouts
(the mutant hung), which is a weak signal. [sin medir] beyond that.

After the new tests (same 15 minute cap, new random order, so only the mutants
that appear in both runs compare):

| Module | Compared | Survived before | Survived after |
|---|---|---|---|
| `registry.py` (without `BitOr`) | 172 | 80 | 53 |
| `ports.py` | 44 | 22 | 22 (the new `check_port` mutants were not drawn in the second run) |

For `ports.py` the run is not comparable; the kills are proven by hand below.

## Weak-test patterns found

- **Constant never checked**: `token_urlsafe(32)`, `[:12]` of the project id,
  `DOCKER_PS_TIMEOUT`, `TERMINATE_TIMEOUT`, `PORTS_TTL`. 20 survivors in `ports.py`, 28
  in `registry.py` are `NumberReplacer`.
- **Boundary**: `len(token) >= 16`, `1 <= port <= 65535`, `max_age > 0`,
  `range(port + 1, ...)`. 12 comparison survivors in `ports.py`, 47 in
  `registry.py`.
- **Branch without a test**: `registry.token` had no test at all (not even the
  env var path); `find_orphans` and `declared_ports` cache branches, the
  `ZeroIterationForLoop` and `continue -> break` survivors (6 in `ports.py`, 10 in
  `registry.py`). `registry.py` survivors in `find_orphans` are partly covered
  from `tests/test_server.py`, which the per-module test-command does not run
  [sin medir].
- **Return not verified**: `remove()` returned `True`/`False` and no test looked.
- **Exception not tested**: `ExceptionReplacer` survivors in the `except
  (psutil.AccessDenied, ...)` handlers (5 in `ports.py`).
- **Boolean operators**: `and`/`or` swapped in `_listeners`, `next_free`,
  `suggest_alternative`, `scan_many` (the port-free check `not in taken and
  _bind_free`) survive: tests never separate the two conditions.

## Tests added and the mutant each kills

Each was verified by applying the mutant by hand, running the test file (fails),
and reverting (passes).

| Test | Mutant |
|---|---|
| `test_token_generado_lleva_32_bytes_de_entropia` | `token_urlsafe(32)` -> `31` |
| `test_token_guardado_se_reutiliza_desde_16_caracteres`, `..._de_15_caracteres_se_regenera` | `len(existing) >= 16` -> `>= 15` |
| `test_token_de_entorno_acepta_16_y_rechaza_15` | `len(from_env) < 16` -> `< 17` |
| `test_token_de_entorno_nuevo_gana_al_legacy` | `STACKHELX_TOKEN or PORTMASTER_TOKEN` -> `and` |
| `test_project_id_son_12_hex` | `hexdigest()[:12]` -> `[:11]` |
| `test_remove_distingue_registrado_de_desconocido` | `len(kept) == len(current)` -> `>`, and `return True` -> `False` |
| `test_check_port_limites_y_bool` | `or` -> `and` (accepts `True`), and `1 <=` -> `2 <=` |

## Accepted survivors

- **Annotation mutants** (`int | None` -> `int + None` and the like, 103 in
  `ports.py`, 22 in `detect.py`, 70 in `registry.py`): equivalent on Python 3.14.
- **Mutants in log and error messages** (`start + limit - 1` in the
  `RuntimeError` of `next_free`): the text is not an interface.
- **`kill(..., force=False)`: `if not force` -> `if force`**: on Windows
  `terminate()` already kills the process, so the branch cannot be reached with a
  real process there. Needs a POSIX child that ignores SIGTERM [sin medir].
- **Timeout constants** (`DOCKER_PS_TIMEOUT`, `TERMINATE_TIMEOUT`, `PORTS_TTL`):
  a test would only copy the constant.

## Not done yet

The remaining survivors of `ports.py` and `registry.py` (boundaries of
`suggest_alternative`, `declared_ports`/`any_uses_docker` cache, `project_for`
offsets) and everything in `detect.py` and `runner.py` beyond the sample.
`survivors` can be listed with the dump command above filtered by
`test_outcome == "survived"`.
