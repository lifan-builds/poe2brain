#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
# Deterministic POSIX/macOS bootstrap for headless Path of Building 2:
#   - LuaJIT 2.1 (commit 2460b3ff93a1c955de3d62cfc825de7d68dc272e, supporting += / ..=)
#   - starwing/luautf8 0.1.6 (lua-utf8.so)
#   - PathOfBuildingCommunity/PathOfBuilding-PoE2 (MIT)
#
# Target layout under ${POE2BRAIN_CACHE_DIR:-$HOME/.cache/poe2brain}/pob2:
#   bin/luajit
#   bin/lua-utf8.so
#   PathOfBuilding-PoE2/   (git checkout containing src/HeadlessWrapper.lua & runtime/lua/)
#   src -> PathOfBuilding-PoE2/src
#   runtime -> PathOfBuilding-PoE2/runtime
#   manifest.json

set -euo pipefail

FORCE=0
if [ "${1:-}" = "--force" ]; then
  FORCE=1
fi

CACHE_ROOT="${POE2BRAIN_CACHE_DIR:-$HOME/.cache/poe2brain}"
POB2_DIR="${CACHE_ROOT}/pob2"
BIN_DIR="${POB2_DIR}/bin"
REPO_DIR="${POB2_DIR}/PathOfBuilding-PoE2"

LUAJIT_COMMIT="2460b3ff93a1c955de3d62cfc825de7d68dc272e"
LUAUTF8_TAG="0.1.6"
POB2_PINNED_COMMIT="${POE2BRAIN_POB2_REF:-601768bd105869618c3d6581b883e5d7aeec91b2}"
POB2_UPSTREAM_URL="https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2.git"

SEED_ROOT="${POE2BRAIN_OSS_SEED_DIR:-/tmp/poe2brain-oss}"
SEED_RUNTIME_BIN="${SEED_ROOT}/posix-runtime/bin"
SEED_REPO_DIR="${SEED_ROOT}/PathOfBuilding-PoE2"

smoke_test_runtime() {
  [ -x "${BIN_DIR}/luajit" ] || return 1
  [ -f "${BIN_DIR}/lua-utf8.so" ] || return 1
  LUA_CPATH="${BIN_DIR}/?.so;;" "${BIN_DIR}/luajit" -e '
    local count = 1
    count += 41
    assert(count == 42, "LuaJIT += operator missing")
    local s = "po"
    s ..= "b2"
    assert(s == "pob2", "LuaJIT ..= operator missing")
    local utf8 = require("lua-utf8")
    assert(utf8.len("流放之路2") == 5, "lua-utf8 failed")
    local ffi = require("ffi")
    local z = ffi.load("z")
    ffi.cdef("const char *zlibVersion(void);")
    assert(#ffi.string(z.zlibVersion()) > 0, "zlib FFI failed")
  ' >/dev/null 2>&1
}

mkdir -p "${BIN_DIR}"

# 1. Provision LuaJIT + lua-utf8.so
if [ "${FORCE}" -eq 0 ] && smoke_test_runtime; then
  echo "[bootstrap_pob2] Existing LuaJIT + lua-utf8 runtime verified at ${BIN_DIR}"
elif [ -x "${SEED_RUNTIME_BIN}/luajit" ] && [ -f "${SEED_RUNTIME_BIN}/lua-utf8.so" ]; then
  echo "[bootstrap_pob2] Seeding prebuilt POSIX runtime from ${SEED_RUNTIME_BIN}"
  cp -f "${SEED_RUNTIME_BIN}/luajit" "${BIN_DIR}/luajit"
  cp -f "${SEED_RUNTIME_BIN}/lua-utf8.so" "${BIN_DIR}/lua-utf8.so"
  chmod +x "${BIN_DIR}/luajit"
  if ! smoke_test_runtime; then
    echo "[bootstrap_pob2] Seed runtime failed smoke test; rebuilding from source..."
    FORCE_BUILD=1
  fi
else
  FORCE_BUILD=1
fi

if [ "${FORCE_BUILD:-0}" -eq 1 ]; then
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/poe2brain-luajit.XXXXXX")"
  trap 'rm -rf "${WORKDIR}"' EXIT

  OS_NAME="$(uname -s)"
  ARCH_NAME="$(uname -m)"
  echo "[bootstrap_pob2] Compiling LuaJIT (${LUAJIT_COMMIT:0:10}) + luautf8 (${LUAUTF8_TAG}) on ${OS_NAME}/${ARCH_NAME}..."

  LUAJIT_SRC="${WORKDIR}/luajit-src"
  mkdir -p "${LUAJIT_SRC}"
  curl -fsSL "https://github.com/LuaJIT/LuaJIT/archive/${LUAJIT_COMMIT}.tar.gz" \
    | tar -xz --strip-components=1 -C "${LUAJIT_SRC}"

  if [ "${OS_NAME}" = "Darwin" ]; then
    export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
    JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  else
    JOBS="$(nproc 2>/dev/null || echo 4)"
  fi

  make -C "${LUAJIT_SRC}/src" -j"${JOBS}" BUILDMODE=static amalg
  cp -f "${LUAJIT_SRC}/src/luajit" "${BIN_DIR}/luajit"
  chmod +x "${BIN_DIR}/luajit"

  LUAUTF8_SRC="${WORKDIR}/luautf8-src"
  mkdir -p "${LUAUTF8_SRC}"
  curl -fsSL "https://github.com/starwing/luautf8/archive/refs/tags/${LUAUTF8_TAG}.tar.gz" \
    | tar -xz --strip-components=1 -C "${LUAUTF8_SRC}"

  CC_BIN="${CC:-cc}"
  if [ "${OS_NAME}" = "Darwin" ]; then
    "${CC_BIN}" -O2 -fPIC -bundle -undefined dynamic_lookup \
      -I"${LUAJIT_SRC}/src" \
      "${LUAUTF8_SRC}/lutf8lib.c" \
      -o "${BIN_DIR}/lua-utf8.so"
  else
    "${CC_BIN}" -O2 -fPIC -shared \
      -I"${LUAJIT_SRC}/src" \
      "${LUAUTF8_SRC}/lutf8lib.c" \
      -o "${BIN_DIR}/lua-utf8.so"
  fi
fi

smoke_test_runtime

# 2. Provision PathOfBuildingCommunity/PathOfBuilding-PoE2 (MIT)
if [ "${FORCE}" -eq 1 ] && [ -d "${REPO_DIR}" ]; then
  rm -rf "${REPO_DIR}"
fi

if [ -f "${REPO_DIR}/src/HeadlessWrapper.lua" ]; then
  echo "[bootstrap_pob2] Existing PathOfBuilding-PoE2 checkout verified at ${REPO_DIR}"
elif [ -d "${SEED_REPO_DIR}/.git" ] && [ -f "${SEED_REPO_DIR}/src/HeadlessWrapper.lua" ]; then
  echo "[bootstrap_pob2] Seeding PathOfBuilding-PoE2 checkout from ${SEED_REPO_DIR}"
  rm -rf "${REPO_DIR}"
  if cp -cR "${SEED_REPO_DIR}" "${REPO_DIR}" 2>/dev/null; then
    :
  else
    cp -a "${SEED_REPO_DIR}" "${REPO_DIR}"
  fi
  git -C "${REPO_DIR}" remote set-url origin "${POB2_UPSTREAM_URL}"
else
  echo "[bootstrap_pob2] Cloning PathOfBuilding-PoE2 (${POB2_PINNED_COMMIT:0:10}) from ${POB2_UPSTREAM_URL}"
  rm -rf "${REPO_DIR}"
  git clone --depth 1 --branch dev "${POB2_UPSTREAM_URL}" "${REPO_DIR}"
  git -C "${REPO_DIR}" fetch --depth 1 origin "${POB2_PINNED_COMMIT}" 2>/dev/null || true
  git -C "${REPO_DIR}" checkout --detach "${POB2_PINNED_COMMIT}" 2>/dev/null || true
fi

ln -sfn "PathOfBuilding-PoE2/src" "${POB2_DIR}/src"
ln -sfn "PathOfBuilding-PoE2/runtime" "${POB2_DIR}/runtime"

POB2_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD)"
cat > "${POB2_DIR}/manifest.json" <<EOF
{
  "luajit_commit": "${LUAJIT_COMMIT}",
  "luautf8_version": "${LUAUTF8_TAG}",
  "pob2_upstream": "${POB2_UPSTREAM_URL}",
  "pob2_license": "MIT",
  "pob2_commit": "${POB2_COMMIT}",
  "luajit_bin": "${BIN_DIR}/luajit",
  "luautf8_lib": "${BIN_DIR}/lua-utf8.so",
  "pob2_repo_dir": "${REPO_DIR}",
  "pob2_src_dir": "${REPO_DIR}/src"
}
EOF

echo "[bootstrap_pob2] Ready: ${POB2_DIR} (PoB2=${POB2_COMMIT:0:10}, LuaJIT=${LUAJIT_COMMIT:0:10})"
