#!/usr/bin/env python3
"""
Benchmark wrapper for Distribicom (distributed, verifiable PIR over SEAL/BGV).

Platform contract:
  * read   /benchmark/config.json
  * write  /benchmark/results.json   (canonical phase/comm schema)

Distribicom is a DISTRIBUTED scheme: a `main_server` hands PIR work to one or more
untrusted `worker`s over gRPC, receives their partial answers, (Freivalds-)verifies
them and aggregates. There is no single-binary "answer a query" call. Instead the
server runs one epoch of 10 rounds over the client query set and logs the wall-clock
time of each round to `server_out_<workers>_workers_<queries>_queries_<r>x<c>.log`
(and to stdout). Those 10 round times are the natural trials for the headline
`server_answer` phase.

This wrapper therefore launches BOTH binaries inside the container on localhost:
  1. start main_server  (binds the port, then blocks waiting for workers)
  2. wait until the port is listening
  3. start worker       (registers with the server; the round loop then runs)
  4. wait for the server to finish its 10 rounds and exit
  5. parse the per-round times from the server log -> server_answer

Because two processes must run concurrently we use subprocess.Popen for both and
let their stdout/stderr inherit our streams (i.e. NO capture_output) so progress is
visible live; structured data is recovered afterwards from the server's own log
file (Distribicom's equivalent of a JSON dump).
"""

import json
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import traceback

CONFIG_PATH = "/benchmark/config.json"
RESULTS_PATH = "/benchmark/results.json"

SERVER_BIN = "/app/bin/main_server"
WORKER_BIN = "/app/bin/worker"

RUN_DIR = "/tmp/distribicom_run"          # server writes its log here (cwd)
PIR_CONFIG_PATH = os.path.join(RUN_DIR, "pir_configs.json")

SCHEME_NAME = "Distribicom"

# --- fixed / compiled-in Distribicom parameters (not exposed by the generic config)
LOGT = 20            # logarithm_plaintext_coefficient (SealPIR-recommended value)
NUM_WORKERS = 1      # single in-container worker
PORT = int(os.environ.get("DISTRIBICOM_PORT", "54321"))

# Distribicom's gRPC message cap (services::constants::max_message_size == 5 MiB).
MAX_MESSAGE_SIZE = 5 * 1024 * 1024

# src/services/utils.cpp:setup_enc_params derives SEAL's coefficient-modulus chain via
# CoeffModulus::BFVDefault(N) (a `// TODO: Use correct BGV parameters` in that file, never
# fixed). At N=1024/2048 that chain is only 1 prime -- too short to support keyswitching,
# so PIRClient::generate_galois_keys throws "keyswitching is not supported by the context"
# inside create_client_db. That exception is swallowed inside a threadpool task, leaving an
# empty query; the worker then fails its matrix multiply and the round never completes, so
# the run hangs until the wrapper's own 3600s timeout instead of failing fast. N=4096 gives
# 3 primes (the minimum that works); 8192 is safer still.
MIN_POLYNOMIAL_DEGREE = 4096


def log(msg):
    print("[wrapper] " + msg, flush=True)


def elements_per_ptxt(logt, N, record_bytes):
    """Replicates SealPIR's elements_per_ptxt(logt, N, ele_size) used by
    services::configurations::create_configs. coefficients_per_element =
    ceil(8*ele_size/logt); packed elements = floor(N / coefficients_per_element)."""
    coeff_per_ele = math.ceil(8 * record_bytes / logt)
    if coeff_per_ele <= 0:
        return 1
    return max(1, N // coeff_per_ele)


def build_pir_config(cfg):
    """Map the generic config onto Distribicom's pir_configs.json fields and derive
    the plaintext-matrix dimensions. Returns (pir_config_dict, mapping_notes)."""
    db = cfg["database"]
    crypto = cfg["crypto"]

    num_records = int(db["num_records"])
    record_bytes = int(db["record_bytes"])
    N = int(crypto["lattice_dimension"])          # RLWE ring degree == polynomial_degree

    epp = elements_per_ptxt(LOGT, N, record_bytes)
    num_ptx_needed = max(1, math.ceil(num_records / epp))
    side = max(1, math.ceil(math.sqrt(num_ptx_needed)))   # square matrix, as in the paper
    db_rows = db_cols = side
    realized_records = side * side * epp

    pir_config = {
        "db_rows": db_rows,
        "db_cols": db_cols,
        "scheme": "bgv",
        "polynomial_degree": N,
        "logarithm_plaintext_coefficient": LOGT,
        "size_per_element": record_bytes,
    }

    notes = [
        "Distribicom stores the DB as a db_rows x db_cols matrix of SEAL/BGV "
        "plaintexts; each plaintext packs elements_per_ptxt = floor(N / "
        "ceil(8*record_bytes/logt)) records. For this run elements_per_ptxt=%d, so "
        "num_records=%d is realised as a %dx%d plaintext matrix holding %d records "
        "(rounded up to a full square matrix)."
        % (epp, num_records, db_rows, db_cols, realized_records),
        "polynomial_degree (SEAL ring degree N) = crypto.lattice_dimension = %d; "
        "it must be a power of two supported by SEAL (1024/2048/4096/8192/16384/32768)."
        % N,
        "logarithm_plaintext_coefficient is fixed at %d (not exposed by the generic "
        "config)." % LOGT,
    ]
    if epp == 1 and math.ceil(8 * record_bytes / LOGT) > N:
        notes.append(
            "WARNING: record_bytes=%d does not fit in a single plaintext for N=%d; "
            "elements_per_ptxt was clamped to 1 and the scheme may reject these "
            "parameters." % (record_bytes, N))
    return pir_config, notes, realized_records


def wait_for_port(host, port, proc, timeout=180.0):
    """Block until (host, port) accepts a TCP connection or the server dies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # server exited before it started listening
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def signal_note(returncode):
    """Human-readable note for a negative (signal-terminated) return code."""
    if returncode >= 0:
        return None
    sig = -returncode
    base = "process terminated by signal %d" % sig
    if sig in (4, 11):  # SIGILL, SIGSEGV
        base += (" (SIGILL/SIGSEGV). Under Rosetta 2 this usually means the binary "
                 "executed an unsupported instruction. Note: Distribicom/SEAL are "
                 "built here without AVX-512, so this should NOT happen for this "
                 "scheme; if it does, rebuild and run on native x86-64.")
    return base


def parse_round_times(pir_config, num_queries):
    """Parse per-round millisecond times from the server log in RUN_DIR."""
    expected = "server_out_%d_workers_%d_queries_%dx%d.log" % (
        NUM_WORKERS, num_queries, pir_config["db_rows"], pir_config["db_cols"])
    candidates = []
    exact = os.path.join(RUN_DIR, expected)
    if os.path.exists(exact):
        candidates = [exact]
    else:
        candidates = [
            os.path.join(RUN_DIR, f)
            for f in os.listdir(RUN_DIR)
            if f.startswith("server_out_") and f.endswith(".log")
        ]
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    if not candidates:
        return [], None

    log_path = candidates[0]
    times = []
    # The server writes one integer-millisecond value per round as "<N>ms," lines
    # inside the results "[ ... ]" block (src/server.cpp).
    line_re = re.compile(r"^\s*(\d+)\s*ms\s*,\s*$")
    with open(log_path, "r", errors="replace") as fh:
        for line in fh:
            m = line_re.match(line)
            if m:
                times.append(float(m.group(1)))
    return times, log_path


def make_error_result(parameters_echo, unmapped, notes, detail):
    return {
        "scheme": SCHEME_NAME,
        "variant": None,
        "status": "error",
        "error": detail,
        "parameters_echo": parameters_echo,
        "phases": {
            "setup": {"mean_ms": None, "note": "run failed before completion"},
            "client_offline_download": {"mean_ms": None, "note": "run failed"},
            "query_generation": {"mean_ms": None, "note": "run failed"},
            "server_answer": {"mean_ms": None, "std_dev_ms": None,
                              "all_trials_ms": [], "num_trials": 0,
                              "note": "run failed: " + detail},
            "client_extract": {"mean_ms": None, "note": "run failed"},
        },
        "communication": {
            "offline_download_bytes": None,
            "query_upload_bytes": None,
            "response_bytes": None,
        },
        "unmapped_parameters": unmapped,
        "notes": notes + [detail],
    }


def write_results(obj):
    with open(RESULTS_PATH, "w") as fh:
        json.dump(obj, fh, indent=2)
    log("wrote %s" % RESULTS_PATH)


def main():
    with open(CONFIG_PATH, "r") as fh:
        cfg = json.load(fh)
    log("config: " + json.dumps(cfg))

    parameters_echo = cfg
    bench = cfg.get("benchmark", {}) or {}
    mode = bench.get("mode", "single")
    batch_size = int(bench.get("batch_size", 1) or 1)

    # ------------------------------------------------------------------ mapping
    pir_config, mapping_notes, realized_records = build_pir_config(cfg)

    if pir_config["polynomial_degree"] < MIN_POLYNOMIAL_DEGREE:
        detail = (
            "lattice_dimension=%d is below Distribicom's minimum of %d and will hang, "
            "not just run slowly: SEAL's coefficient-modulus chain at this size doesn't "
            "support keyswitching, so Galois key generation fails silently inside a "
            "worker thread and the round never completes. Use lattice_dimension >= %d "
            "(4096 is the minimum that works; 8192 is safer)."
            % (pir_config["polynomial_degree"], MIN_POLYNOMIAL_DEGREE,
               MIN_POLYNOMIAL_DEGREE))
        log("refusing to run: " + detail)
        write_results(make_error_result(parameters_echo, {}, mapping_notes, detail))
        return 1

    # Batch handling. Distribicom natively amortises server work across many
    # concurrent single-record queries (num_queries clients per round). We map
    # batch_size -> num_queries: "batch" answers batch_size independent
    # single-record queries per round (NOT one query fetching many records).
    if mode == "batch" and batch_size > 1:
        num_queries = batch_size
        batch_note = ("mode=batch: mapped batch_size=%d -> num_queries (Distribicom "
                      "answers %d concurrent single-record queries per round; "
                      "throughput = records-per-round / round-time)."
                      % (batch_size, batch_size))
    else:
        num_queries = 1
        batch_note = ("mode=%s: running a single query per round (num_queries=1); "
                      "batch_size=%d." % (mode, batch_size))
    mapping_notes.append(batch_note)

    # Every generic input parameter is forwarded to the binary, so there are no
    # unmapped parameters. (logarithm_plaintext_coefficient is a fixed compiled
    # value, not part of the generic input.)
    unmapped_parameters = {}

    # ---------------------------------------------------------------- prepare run
    if os.path.isdir(RUN_DIR):
        for f in os.listdir(RUN_DIR):
            if f.startswith("server_out_") and f.endswith(".log"):
                try:
                    os.remove(os.path.join(RUN_DIR, f))
                except OSError:
                    pass
    else:
        os.makedirs(RUN_DIR, exist_ok=True)

    with open(PIR_CONFIG_PATH, "w") as fh:
        json.dump(pir_config, fh)
    log("pir_configs.json: " + json.dumps(pir_config))

    nproc = str(os.cpu_count() or 4)
    server_host = "localhost:%d" % PORT

    server_cmd = [SERVER_BIN, PIR_CONFIG_PATH, str(num_queries),
                  str(NUM_WORKERS), nproc, server_host]
    worker_cmd = [WORKER_BIN, PIR_CONFIG_PATH, nproc, server_host]

    log("server cmd: " + " ".join(server_cmd))
    log("worker cmd: " + " ".join(worker_cmd))

    # ------------------------------------------------------------- launch server
    # No capture_output: stdout/stderr stream live for progress. Structured data
    # is read back from the server log file afterwards.
    server_proc = subprocess.Popen(server_cmd, cwd=RUN_DIR)

    if not wait_for_port("127.0.0.1", PORT, server_proc, timeout=180.0):
        rc = server_proc.poll()
        if rc is None:
            server_proc.kill()
            server_proc.wait()
            detail = "server did not start listening on port %d within timeout" % PORT
        else:
            detail = "server exited (code %d) before listening; %s" % (
                rc, signal_note(rc) or "check the log above")
        write_results(make_error_result(parameters_echo, unmapped_parameters,
                                        mapping_notes, detail))
        return 1

    # ------------------------------------------------------------- launch worker
    worker_proc = subprocess.Popen(worker_cmd, cwd=RUN_DIR)

    # ------------------------------------------------ wait for the epoch to finish
    try:
        server_ret = server_proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        server_proc.kill()
        server_proc.wait()
        _terminate(worker_proc)
        detail = "server run exceeded 3600s timeout"
        write_results(make_error_result(parameters_echo, unmapped_parameters,
                                        mapping_notes, detail))
        return 1

    # Server finished its 10 rounds and sent the stop signal; give the worker a
    # moment to unwind its stream, then make sure it is gone.
    _terminate(worker_proc, grace=15)

    if server_ret != 0:
        detail = "main_server exited with code %d. %s" % (
            server_ret, signal_note(server_ret) or "")
        write_results(make_error_result(parameters_echo, unmapped_parameters,
                                        mapping_notes, detail.strip()))
        return 1

    # ------------------------------------------------------------- parse results
    trials, log_path = parse_round_times(pir_config, num_queries)
    if not trials:
        detail = ("server completed but no per-round timings were found in the log "
                  "(%s)" % (log_path or "no server_out_*.log produced"))
        write_results(make_error_result(parameters_echo, unmapped_parameters,
                                        mapping_notes, detail))
        return 1

    mean_ms = statistics.mean(trials)
    std_ms = statistics.stdev(trials) if len(trials) > 1 else None
    log("parsed %d round times from %s (mean=%.3f ms)" % (len(trials), log_path, mean_ms))

    server_answer_note = (
        "Headline metric: wall-clock time of one distributed PIR round answering "
        "%d query/queries. Each round covers server->worker work distribution, the "
        "worker's homomorphic computation, Freivalds verification of the worker's "
        "answer (compiled in by default), and server-side step-2 aggregation. "
        "%d rounds of a single epoch are reported as trials. Values are already in "
        "milliseconds (integer resolution as emitted by the server)."
        % (num_queries, len(trials)))

    result = {
        "scheme": SCHEME_NAME,
        "variant": None,
        "parameters_echo": parameters_echo,
        "phases": {
            "setup": {
                "mean_ms": None,
                "note": ("Distribicom performs one-time setup before the epoch "
                         "(client creation, Galois-key + query generation, DB "
                         "plaintext encoding) but does not emit it as a discrete "
                         "timed metric, so it is not reported."),
            },
            "client_offline_download": {
                "mean_ms": None,
                "note": ("No separate client offline-download phase is timed; the "
                         "server binary does not measure hint/key download."),
            },
            "query_generation": {
                "mean_ms": None,
                "note": ("Client queries are generated during server setup "
                         "(create_client_db) and are not timed separately by the "
                         "binary."),
            },
            "server_answer": {
                "mean_ms": mean_ms,
                "std_dev_ms": std_ms,
                "all_trials_ms": trials,
                "num_trials": len(trials),
                "note": server_answer_note,
            },
            "client_extract": {
                "mean_ms": None,
                "note": ("Client-side answer decoding runs only in DISTRIBICOM_DEBUG "
                         "builds (verify_results) and is not timed in this Release "
                         "build."),
            },
        },
        "communication": {
            "offline_download_bytes": None,
            "query_upload_bytes": None,
            "response_bytes": None,
        },
        "unmapped_parameters": unmapped_parameters,
        "notes": mapping_notes + [
            "server_answer includes Freivalds verification of the untrusted worker "
            "(FREIVALDS is ON by default in the CMake build).",
            "Communication sizes are not emitted by main_server (it logs only round "
            "timings), so they are reported as null rather than fabricated.",
            "Very large num_records / record_bytes may exceed Distribicom's %d-byte "
            "gRPC message cap and cause the run to fail." % MAX_MESSAGE_SIZE,
            "Ran %d worker(s) and %d server/worker thread(s) on localhost inside the "
            "container." % (NUM_WORKERS, os.cpu_count() or 4),
        ],
    }
    write_results(result)
    return 0


def _terminate(proc, grace=5):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never leave the volume without a results.json
        traceback.print_exc()
        try:
            echo = {}
            try:
                with open(CONFIG_PATH) as fh:
                    echo = json.load(fh)
            except Exception:
                pass
            write_results(make_error_result(
                echo, {}, [], "wrapper crashed: %r" % exc))
        except Exception:
            pass
        sys.exit(1)
