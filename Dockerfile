# ==============================================================================
# Distribicom — distributed PIR benchmark image
#
# Force x86_64 so the same image runs on the Apple-Silicon dev box (under Rosetta,
# for pipeline validation) and on the native x86_64 benchmark server.
#
# NOTE: Distribicom is a C++20 / CMake project (Microsoft SEAL v4.0.0 + a SealPIR
# fork + gRPC), NOT a Rust project, so the RUSTFLAGS / build.rs precautions do not
# apply here. SEAL is built WITHOUT Intel HEXL (SEAL_USE_INTEL_HEXL is off in
# dependencies/FetchSeal.cmake), and nothing in the tree uses -march=native or
# AVX-512 intrinsics -> the binaries do NOT require AVX-512 at runtime.
# ==============================================================================
FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Toolchain. Ubuntu 22.04 ships gcc/g++ 11 (>= 11.2 as Distribicom requires) and
# cmake 3.22 (>= 3.20). git is needed because the CMake build fetches SEAL,
# SealPIR and gRPC (with submodules) from source at configure/build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        g++ \
        cmake \
        git \
        pkg-config \
        libssl-dev \
        autoconf \
        automake \
        libtool \
        m4 \
        curl \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------------------
# x86 SIMD targeting.
#
# We want AVX2 auto-vectorisation for SEAL's NTT and SealPIR's matrix math on the
# x86_64 server, so target -march=x86-64-v3 (AVX2). We do NOT use x86-64-v4
# (AVX-512): the gRPC build compiles and RUNS host tools (protoc, grpc_cpp_plugin)
# during `docker build`; on Apple Silicon that build happens under Rosetta, which
# emulates up to AVX2 only -> AVX-512 codegen in those tools would SIGILL at build
# time. AVX2 (v3) runs fine under Rosetta, so the build is safe.
#
# The top-level CMakeLists.txt does `set(CMAKE_CXX_FLAGS " -O3")`, which OVERWRITES
# any -DCMAKE_CXX_FLAGS we might pass. To inject -march reliably across the main
# project AND the in-tree gRPC/SealPIR subprojects, we wrap the compilers instead
# of relying on CMAKE_CXX_FLAGS. The wrappers append -march to every invocation.
# ------------------------------------------------------------------------------
RUN printf '#!/bin/sh\nexec /usr/bin/gcc -march=x86-64-v3 "$@"\n' > /usr/local/bin/cc-v3 && \
    printf '#!/bin/sh\nexec /usr/bin/g++ -march=x86-64-v3 "$@"\n' > /usr/local/bin/cxx-v3 && \
    chmod +x /usr/local/bin/cc-v3 /usr/local/bin/cxx-v3
ENV CC=/usr/local/bin/cc-v3
ENV CXX=/usr/local/bin/cxx-v3

# Copy the LOCAL source tree (may contain local fixes not upstream). No git clone.
COPY . /app
WORKDIR /app

# Build only the two executables we benchmark (main_server + worker). This still
# builds their transitive deps (SEAL, SealPIR, gRPC) but skips the test suite.
# CMAKE_BUILD_TYPE=Release keeps DISTRIBICOM_DEBUG off (no per-round result
# verification/printing) while leaving FREIVALDS on (its CMake default) so the
# measured round reflects Distribicom's verified distributed answer.
# Binaries are emitted to /app/bin per CMAKE_RUNTIME_OUTPUT_DIRECTORY.
RUN cmake -S /app -B /app/build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build /app/build --target main_server worker -j"$(nproc)"

# Volume mount point for the platform contract (config.json in, results.json out).
RUN mkdir -p /benchmark

# Wrapper reads /benchmark/config.json and writes /benchmark/results.json.
ENTRYPOINT ["python3", "/app/wrapper.py"]
