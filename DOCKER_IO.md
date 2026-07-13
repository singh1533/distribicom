# Docker Input / Output Reference

This document describes exactly what the Docker container ([Dockerfile](Dockerfile)) reads,
what it writes, and how config values map onto the underlying **Distribicom**
distributed-PIR benchmark (C++20 / CMake, Microsoft SEAL v4.0.0 BGV + a SealPIR fork +
gRPC). The container's entrypoint is [wrapper.py](wrapper.py), which orchestrates two
compiled binaries: `main_server` and `worker`.

Distribicom is fundamentally different from a single-binary PIR scheme: a `main_server`
hands homomorphic work to one or more untrusted `worker`s over gRPC, receives their
partial answers, Freivalds-verifies them, and aggregates. There is no "answer a query"
function call to time directly — instead the server runs one epoch of **10 rounds** over
a fixed client-query set and logs the wall-clock time of each round. The wrapper starts
both processes, waits for the epoch to finish, and parses those 10 round times out of the
server's log file.

## How it runs

```
docker run -v /path/to/shared_volume:/benchmark <image>
```

- **Input** is read from `/benchmark/config.json` (inside the container).
- **Output** is written to `/benchmark/results.json` (inside the container).
- Mount a host directory (e.g. [shared_volume/](shared_volume/)) to `/benchmark`.
- Image is forced to `linux/amd64` ([Dockerfile:13](Dockerfile#L13)) so it also runs
  under Rosetta 2 on Apple Silicon dev boxes; compiled with `-march=x86-64-v3` (AVX2,
  not AVX-512) via compiler wrappers so it doesn't SIGILL under Rosetta
  ([Dockerfile:36-55](Dockerfile#L36-L55)).
- Build type is `CMAKE_BUILD_TYPE=Release` ([Dockerfile:67](Dockerfile#L67)), which keeps
  `DISTRIBICOM_DEBUG` **off** (see below) while leaving `FREIVALDS` **on** (its CMake
  default — [util/CMakeLists.txt](util/CMakeLists.txt)).

Internally, `wrapper.py` (`main()`, [wrapper.py:214](wrapper.py#L214)):
1. Writes a derived `pir_configs.json` to `/tmp/distribicom_run/` ([wrapper.py:259](wrapper.py#L259)).
2. Launches `main_server <pir_configs.json> <num_queries> <num_workers=1> <nproc> localhost:54321` ([wrapper.py:266](wrapper.py#L266)).
3. Waits for the server's TCP port to open (`wait_for_port`, [wrapper.py:122](wrapper.py#L122)), timeout 180s.
4. Launches `worker <pir_configs.json> <nproc> localhost:54321` ([wrapper.py:268](wrapper.py#L268)).
5. Waits (up to 3600s) for `main_server` to run its 10-round epoch and exit ([wrapper.py:296](wrapper.py#L296)); the worker is registered and driven entirely by the server — the wrapper never sends it a separate command.
6. Terminates the worker, then parses `server_out_<workers>_workers_<queries>_queries_<r>x<c>.log` for `"<N>ms,"` lines (`parse_round_times`, [wrapper.py:150](wrapper.py#L150)), one per round, exactly matching the format the server writes at [src/server.cpp:182](src/server.cpp#L182).

If the server never opens its port, exits non-zero, or the run produces no parseable
round-time lines, the wrapper writes an **error result** (`make_error_result`,
[wrapper.py:182](wrapper.py#L182)) with `status: "error"` and all phase fields `null` —
it never leaves the volume without a `results.json`, even on an uncaught Python
exception ([wrapper.py:411-427](wrapper.py#L411-L427)).

## Input: `config.json`

Example ([shared_volume/config.json](shared_volume/config.json)):

```json
{
  "database":  { "num_records": 256, "record_bytes": 256 },
  "crypto":    { "lattice_dimension": 4096 },
  "benchmark": { "mode": "single", "batch_size": 1 }
}
```

| Key | Required | Used? | Description |
|---|---|---|---|
| `database.num_records` | Yes | ✅ Yes | Target record count. **Not used directly as a scheme parameter** — it drives a derivation to a `db_rows x db_cols` plaintext matrix (see below). The realized capacity is **rounded up**, generally to more than `num_records` (see "Gotchas"). |
| `database.record_bytes` | Yes | ✅ Yes | Passed straight through as `size_per_element` in the derived `pir_configs.json` ([wrapper.py:98](wrapper.py#L98)), which becomes `Configs.size_per_element` and thus `ele_size` in SealPIR's `elements_per_ptxt` ([src/services/factory.cpp:18](src/services/factory.cpp#L18)). Also used by the wrapper itself to size the plaintext matrix (`elements_per_ptxt`, [wrapper.py:66](wrapper.py#L66)). |
| `crypto.lattice_dimension` | Yes | ✅ Yes | Mapped 1:1 to `polynomial_degree` (the SEAL BGV ring degree `N`) ([wrapper.py:96](wrapper.py#L96) → `services::utils::setup_enc_params`, [src/services/utils.cpp:79-89](src/services/utils.cpp#L79-L89)). Must be a power of two SEAL supports: 1024/2048/4096/8192/16384/32768 (wrapper comment, [wrapper.py:109](wrapper.py#L109); not independently range-checked by the wrapper — an unsupported value will cause the SEAL/gRPC binary to fail at parameter-setup time, surfacing as a non-zero `main_server` exit and an error `results.json`). |
| `benchmark.mode` | No (defaults `"single"`) | ✅ Yes, but only two effective states | Only the literal string `"batch"` (combined with `batch_size > 1`) changes behavior ([wrapper.py:231](wrapper.py#L231)); any other value (including unset, `"single"`, or a typo like `"Batch"`) falls through to the `else` branch (`num_queries = 1`). No warning is emitted for typos — they silently behave as `"single"`. |
| `benchmark.batch_size` | No (defaults `1`) | ✅ Yes, conditionally | Only takes effect when `mode == "batch"` **and** `batch_size > 1`; then it is mapped to `num_queries` (the number of *concurrent single-record* queries answered per round — **not** one query fetching `batch_size` records) ([wrapper.py:231-241](wrapper.py#L231-L241)). If `mode` is not `"batch"`, `batch_size` is read but then ignored (`num_queries` is forced to `1` regardless of its value). |

### Fixed / compiled-in parameters not exposed by config.json

These are hardcoded in `wrapper.py` itself, not read from `config.json` at all:

| Constant | Value | Where | Notes |
|---|---|---|---|
| `LOGT` (`logarithm_plaintext_coefficient`) | `20` | [wrapper.py:54](wrapper.py#L54) | SealPIR-recommended value. Forwarded into `pir_configs.json` and thence into `Configs.logarithm_plaintext_coefficient` ([src/services/factory.cpp:9](src/services/factory.cpp#L9)), which feeds `elements_per_ptxt` and `PlainModulus::Batching(N, logt+1)` ([src/services/utils.cpp:87](src/services/utils.cpp#L87)). There is no config key for this. |
| `NUM_WORKERS` | `1` | [wrapper.py:55](wrapper.py#L55) | Only one worker container process is ever started; passed as both `<num_workers>` to `main_server` and hardcoded into the log-filename pattern the parser expects. |
| `PORT` | `54321` (overridable only via the `DISTRIBICOM_PORT` **environment** variable, not `config.json`) | [wrapper.py:56](wrapper.py#L56) | Localhost-only; not a network benchmark parameter. |
| `pir_config["scheme"]` | `"bgv"` | [wrapper.py:95](wrapper.py#L95) | Hardcoded; matches `create_configs`'s own hardcoded `c.mutable_scheme()->assign("bgv")` ([src/services/factory.cpp:6](src/services/factory.cpp#L6)) — there is no other scheme option in this codebase. |
| `dimensions` | `2` | [src/services/factory.cpp:15](src/services/factory.cpp#L15) | SealPIR recursion depth; set unconditionally inside `create_configs`, not passed through `pir_configs.json` at all (the wrapper's `pir_config` dict has no `dimensions` key — the C++ default proto field wins). |
| Number of rounds per epoch | `10` | [src/server.cpp:141](src/server.cpp#L141) (`for (int j = 0; j < 10; ++j)`) | Hardcoded loop bound in `main_server`'s `main()`. Also duplicated as the `query_wait_time` argument (`10`) passed into `create_app_configs` at [src/server.cpp:82](src/server.cpp#L82) and [src/worker.cpp:41](src/worker.cpp#L41) — unrelated to round count despite the coincidental value, it configures a different app-config field. |
| `FREIVALDS` | ON | [util/CMakeLists.txt](util/CMakeLists.txt) `option(FREIVALDS "Enable Freivalds' algorithm" ON)` | CMake option, defaulted ON and not overridden by the Dockerfile build command, so every measured round includes Freivalds verification of the untrusted worker's answer. |
| `DISTRIBICOM_DEBUG` | OFF | [util/CMakeLists.txt](util/CMakeLists.txt): only set when `CMAKE_BUILD_TYPE STREQUAL "Debug"` | Dockerfile builds `-DCMAKE_BUILD_TYPE=Release`, so this stays off. Gates `verify_results()` at [src/server.cpp:174-176](src/server.cpp#L174-L176) — see Output section. |
| gRPC max message size | 5 MiB (`5 * 1024 * 1024`) | [src/services/constants.hpp:17](src/services/constants.hpp#L17) `max_message_size`, applied via `builder.SetMaxMessageSize(...)` at [src/server.cpp:212](src/server.cpp#L212) | Not configurable via `config.json`. Large `num_records`/`record_bytes` combinations can exceed this and cause the run to fail (wrapper surfaces this as a note, [wrapper.py:387-388](wrapper.py#L387-L388), but does not pre-validate it). |

### How `num_records` / `record_bytes` / `lattice_dimension` actually reach the binaries

`build_pir_config()` ([wrapper.py:76](wrapper.py#L76)) derives a `pir_configs.json` (SealPIR/Distribicom's own config file format, distinct from the platform's `config.json`) that both `main_server` and `worker` load at startup via `google::protobuf::util::JsonStringToMessage` ([src/server.cpp:74](src/server.cpp#L74), [src/worker.cpp:33](src/worker.cpp#L33)):

```
coeff_per_ele    = ceil(8 * record_bytes / LOGT)              # LOGT fixed = 20
elements_per_ptxt (epp) = max(1, N // coeff_per_ele)           # N = lattice_dimension
num_ptx_needed   = ceil(num_records / epp)
side             = ceil(sqrt(num_ptx_needed))                  # square matrix
db_rows = db_cols = side
realized_records = side * side * epp                           # >= num_records, rounded up
```

This replicates SealPIR's own `elements_per_ptxt` (an external, fetched dependency —
see [dependencies/FetchSealPIR.cmake](dependencies/FetchSealPIR.cmake) — not present in
this source tree, but consumed identically server-side in `create_configs`,
[src/services/factory.cpp:9-20](src/services/factory.cpp#L9-L20): `num_elements_in_ptx =
elements_per_ptxt(logt, poly_deg, ele_size); num_elements = rows * cols *
num_elements_in_ptx`). The wrapper's Python replica and the C++ server's computation use
the same formula and the same fixed `logt=20`, so they agree — this was verified by
reading both implementations side by side.

`pir_configs.json` written to disk only contains: `db_rows`, `db_cols`, `scheme`,
`polynomial_degree`, `logarithm_plaintext_coefficient`, `size_per_element`. `dimensions`
(`2`), `use_batching` (`true`) and `use_recursive_mod_switching` (`false`) are filled in
by hardcoded values inside `create_configs` regardless of what's in the JSON file
([src/services/factory.cpp:15-19](src/services/factory.cpp#L15-L19)) — there is no way to
set them from either `config.json` or `pir_configs.json`.

### Gotchas

- **`num_records` is a lower bound, not an exact count.** Because the DB is packed into a
  square matrix of plaintexts, `realized_records` (`side * side * epp`) can be
  substantially larger than the requested `num_records` — e.g. requesting 256 records
  may realize far more once rounded up to a full square matrix of SEAL plaintexts. The
  wrapper logs this in `mapping_notes` ([wrapper.py:101-107](wrapper.py#L101-L107)) but
  does **not** put it in `results.json`'s numeric fields — only in the free-text `notes`
  array.
- **`record_bytes` too large for `N` silently clamps `epp` to 1**, and if it still doesn't
  fit, the wrapper emits a `WARNING:` string into `notes` ([wrapper.py:114-118](wrapper.py#L114-L118))
  but still attempts the run — the actual failure (if any) surfaces later as a non-zero
  `main_server` exit code, not as a pre-flight validation error.
- **`batch_size` without `mode: "batch"` is silently ignored** — set `batch_size: 8` with
  `mode: "single"` (or omitted `mode`) and you still get `num_queries = 1`.
- **`mode: "batch"` does not batch a single query across multiple records** — it runs
  `batch_size` independent single-record queries concurrently in one round. If you expect
  "one query fetching many records," this config does not produce that.
- **No parameter-range validation happens in the wrapper before launching the binaries.**
  Invalid combinations (e.g. `lattice_dimension` not a supported SEAL power of two, or a
  message size exceeding the 5 MiB gRPC cap) are only caught when `main_server` itself
  fails, at which point the wrapper reports a generic `"main_server exited with code
  N"` error rather than a specific validation message.
- **Signal-termination diagnostics are Rosetta-specific.** If `main_server`/`worker` die
  from SIGILL/SIGSEGV, the wrapper appends a note suggesting an unsupported instruction
  under Rosetta 2 — but also states this "should NOT happen for this scheme" since AVX-512
  is deliberately avoided ([wrapper.py:136-147](wrapper.py#L136-L147)).

## Output: `results.json`

Success shape ([wrapper.py:339-393](wrapper.py#L339-L393)):

```json
{
  "scheme": "Distribicom",
  "variant": null,
  "parameters_echo": { ...original config.json... },
  "phases": {
    "setup":                    { "mean_ms": null, "note": "..." },
    "client_offline_download":  { "mean_ms": null, "note": "..." },
    "query_generation":         { "mean_ms": null, "note": "..." },
    "server_answer":            { "mean_ms": 0.0, "std_dev_ms": 0.0, "all_trials_ms": [...], "num_trials": 10, "note": "..." },
    "client_extract":           { "mean_ms": null, "note": "..." }
  },
  "communication": {
    "offline_download_bytes": null,
    "query_upload_bytes": null,
    "response_bytes": null
  },
  "unmapped_parameters": {},
  "notes": [ "..." ]
}
```

On failure (server never starts, non-zero exit, or no parseable log), `status: "error"`
and `error: "<detail>"` are added, and **every** phase field including `server_answer` is
`null` / empty ([wrapper.py:182-205](wrapper.py#L182-L205)).

### `phases` — is each field real?

| Field | Measured? | Notes |
|---|---|---|
| `setup.mean_ms` | ❌ **No — always `null`** | Distribicom performs real one-time setup (client creation, Galois-key + query generation, DB plaintext encoding — `create_clients`/`create_client_db`, [src/server.cpp:239-315](src/server.cpp#L239-L315)) before the timed loop starts, but `main_server` never emits this as a discrete timed metric to stdout or the log file, so the wrapper has nothing to parse and reports `null` rather than fabricating a number ([wrapper.py:344-350](wrapper.py#L344-L350)). |
| `client_offline_download.mean_ms` | ❌ **No — always `null`** | No hint/key-download phase is timed by the binary at all ([wrapper.py:351-355](wrapper.py#L351-L355)). |
| `query_generation.mean_ms` | ❌ **No — always `null`** | Query generation happens inside `create_client_db` during setup (same untimed block as above) and is not isolated or timed separately ([wrapper.py:356-361](wrapper.py#L356-L361)). |
| `server_answer.mean_ms` / `std_dev_ms` / `all_trials_ms` / `num_trials` | ✅ **Yes — the only real measured metric** | Parsed from the 10 `"<N>ms,"` lines `main_server` writes per round in `server_out_*.log` ([src/server.cpp:141-183](src/server.cpp#L141-L183)). Computed via `statistics.mean`/`statistics.stdev` over all 10 rounds ([wrapper.py:326-327](wrapper.py#L326-L327)) — **no warmup round is excluded** (unlike some sibling schemes' benchmarks). Each round's timed span covers: `distribute_work` → wait for worker completion (`ledger->done.read()`) → `learn_about_rouge_workers` (Freivalds verification of the untrusted worker's partial answer, since `FREIVALDS` is compiled ON) → `run_step_2` (server-side aggregation). Integer millisecond resolution, as emitted by `std::chrono::duration_cast<milliseconds>` in the C++ code. |
| `client_extract.mean_ms` | ❌ **No — always `null`** | The actual decode step exists as `verify_results()` ([src/server.cpp:197-203](src/server.cpp#L197-L203), calls `clients[id].decode_reply(...)`) but is compiled **only** under `#ifdef DISTRIBICOM_DEBUG` ([src/server.cpp:174-176](src/server.cpp#L174-L176)). Since the Dockerfile builds `CMAKE_BUILD_TYPE=Release` (not `Debug`), `DISTRIBICOM_DEBUG` is undefined, so this code path is **not compiled into the binary at all** — it isn't merely skipped at runtime, it doesn't exist in the shipped `main_server`. Even when compiled in, it only prints decoded plaintexts to stdout; it was never timed even in debug builds. |

### `communication` — is each field real?

| Field | Measured? | Notes |
|---|---|---|
| `offline_download_bytes` | ❌ **No — always `null`** | |
| `query_upload_bytes` | ❌ **No — always `null`** | |
| `response_bytes` | ❌ **No — always `null`** | `main_server`/`worker` log only round timings, never message/payload sizes, to stdout or the log file ([wrapper.py:376-380](wrapper.py#L376-L380), [wrapper.py:385-386](wrapper.py#L385-L386)). The wrapper explicitly chooses to report `null` here rather than estimate/fabricate a value from the derived matrix shape. |

### `unmapped_parameters`

Always `{}` on success — every field consumed from `config.json` maps onto something
forwarded to the binaries; the only genuinely fixed value (`logarithm_plaintext_coefficient
= 20`) is a *compiled* constant not part of the generic input schema in the first place,
so it isn't counted as an "ignored input" ([wrapper.py:243-246](wrapper.py#L243-L246)).

### To get the values that are `null` here

There is no alternate run mode in this repository that measures `setup`,
`client_offline_download`, `query_generation`, or communication sizes — those phases are
architecturally never surfaced by `main_server`/`worker` (not just suppressed by the
wrapper). The closest partial exception is `client_extract`: building with
`-DCMAKE_BUILD_TYPE=Debug` (or `-DDISTRIBICOM_DEBUG=ON`) compiles in `verify_results()`,
which will *print* decoded answers to stdout for correctness checking — but it still does
not time that step, and it also changes the binary being benchmarked (adds decode work
inside the same measured round to catch a `worker_bench` benchmark loop). Genuine
per-phase instrumentation would require modifying `src/server.cpp` / `src/worker.cpp` to
emit additional timed log lines.

## Summary: fixed vs. configurable

| Configurable via `config.json` | Fixed / not configurable |
|---|---|
| `database.num_records` (rounded **up** to a full square plaintext matrix) | `logarithm_plaintext_coefficient` (`LOGT = 20`, hardcoded in `wrapper.py`) |
| `database.record_bytes` (→ `size_per_element`) | `scheme` (always `"bgv"`) |
| `crypto.lattice_dimension` (→ `polynomial_degree`; must be a SEAL-supported power of two) | `dimensions` (always `2`, SealPIR recursion depth) |
| `benchmark.mode` (`"single"` vs. `"batch"`; anything else silently behaves as `"single"`) | `use_batching` (always `true`), `use_recursive_mod_switching` (always `false`) |
| `benchmark.batch_size` (only effective when `mode: "batch"`) | `NUM_WORKERS` (always `1` worker process) |
| | Number of epoch rounds (always `10`, hardcoded loop bound) |
| | `FREIVALDS` (compiled ON — every round is verified) |
| | `DISTRIBICOM_DEBUG` / client decode-and-verify step (compiled OFF in Release build — not present in the shipped binary) |
| | gRPC max message size (5 MiB, `services::constants::max_message_size`) |
| | Port (`54321`, overridable only via `DISTRIBICOM_PORT` env var, not `config.json`) |
| | All communication-size fields in `results.json` (never emitted by the binaries — always `null`) |
| | `setup`, `client_offline_download`, `query_generation`, `client_extract` timings (never emitted by the binaries — always `null`; only `server_answer` is real) |
