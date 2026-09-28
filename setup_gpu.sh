#!/usr/bin/env bash

# Source this file from the project root: source setup_gpu.sh
# The shell hosts only the lightweight agent runtime. Scientific GPU work is
# dispatched to explicit physical runtimes by the environment broker.
_SCAGENT_SDK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_SCAGENT_SDK_UV="$(command -v uv 2>/dev/null || true)"

if [[ -z "${_SCAGENT_SDK_UV}" ]]; then
  for _SCAGENT_SDK_UV_CANDIDATE in \
    "${HOME}/.local/bin/uv" \
    "/data1/peerd/${USER}/tools/miniconda3/bin/uv" \
    "/usersoftware/peerd/${USER}/bin/uv"; do
    if [[ -x "${_SCAGENT_SDK_UV_CANDIDATE}" ]]; then
      _SCAGENT_SDK_UV="${_SCAGENT_SDK_UV_CANDIDATE}"
      break
    fi
  done
fi

if [[ -z "${_SCAGENT_SDK_UV}" || ! -x "${_SCAGENT_SDK_UV}" ]]; then
  echo "scagent-sdk setup requires uv (install it or add it to PATH)" >&2
  return 1 2>/dev/null || exit 1
fi

_SCAGENT_SDK_PIXI="${SCAGENT_SDK_PIXI:-$(command -v pixi 2>/dev/null || true)}"
if [[ -z "${_SCAGENT_SDK_PIXI}" ]]; then
  for _SCAGENT_SDK_PIXI_CANDIDATE in \
    "${HOME}/.pixi/bin/pixi" \
    "/usersoftware/peerd/${USER}/.pixi/bin/pixi"; do
    if [[ -x "${_SCAGENT_SDK_PIXI_CANDIDATE}" ]]; then
      _SCAGENT_SDK_PIXI="${_SCAGENT_SDK_PIXI_CANDIDATE}"
      break
    fi
  done
fi

if [[ -z "${_SCAGENT_SDK_PIXI}" || ! -x "${_SCAGENT_SDK_PIXI}" ]]; then
  echo "scagent-sdk setup requires Pixi" >&2
  return 1 2>/dev/null || exit 1
fi

# Remove an already-active Python venv before activating this project's venv.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  deactivate 2>/dev/null || true
  PATH="$(printf '%s' "${PATH}" | tr ':' '\n' | grep -vxF "${VIRTUAL_ENV}/bin" | paste -sd ':' -)"
  export PATH
  unset VIRTUAL_ENV
fi

# Conda base is often auto-activated on Iris. It is not the agent runtime and
# must not leak CONDA_PREFIX/PATH state into broker workers.
if declare -F conda >/dev/null 2>&1; then
  while [[ "${CONDA_SHLVL:-0}" -gt 0 ]]; do
    conda deactivate >/dev/null 2>&1 || break
  done
elif [[ -n "${CONDA_PREFIX:-}" ]]; then
  PATH="$(printf '%s' "${PATH}" | tr ':' '\n' | grep -vxF "${CONDA_PREFIX}/bin" | paste -sd ':' -)"
  export PATH
  unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL
fi

SCAGENT_SDK_UV="${_SCAGENT_SDK_UV}" \
  "${_SCAGENT_SDK_ROOT}/scripts/bootstrap_agent.sh" || {
    return 1 2>/dev/null || exit 1
  }

SCAGENT_SDK_PIXI="${_SCAGENT_SDK_PIXI}" \
  "${_SCAGENT_SDK_ROOT}/scripts/bootstrap_compute.sh" || {
  return 1 2>/dev/null || exit 1
}

source "${_SCAGENT_SDK_ROOT}/.venv/bin/activate"

# `scagent` is the OpenCode-backed interface. Keep this repository's launcher
# ahead of the historical Python-console alias installed in the venv;
# `scagent-sdk` remains the explicit native SDK terminal.
PATH="${_SCAGENT_SDK_ROOT}/scripts:${PATH}"
export PATH

export SCAGENT_SDK_PROJECT_ROOT="${_SCAGENT_SDK_ROOT}"
export SCAGENT_SDK_UV="${_SCAGENT_SDK_UV}"
export SCAGENT_SDK_PIXI="${_SCAGENT_SDK_PIXI}"
export SCAGENT_SDK_MODEL_PROFILE="${SCAGENT_SDK_MODEL_PROFILE:-iris-qwen36}"
export SCAGENT_SDK_MODEL_PROFILES_DIR="${SCAGENT_SDK_MODEL_PROFILES_DIR:-${_SCAGENT_SDK_ROOT}/configs/models}"
export SCAGENT_SDK_SKILLS_DIR="${SCAGENT_SDK_SKILLS_DIR:-${_SCAGENT_SDK_ROOT}/.claude/skills}"
export SCAGENT_SDK_SESSIONS_DIR="${SCAGENT_SDK_SESSIONS_DIR:-${_SCAGENT_SDK_ROOT}/sessions}"
export SCAGENT_SDK_ENVIRONMENTS_FILE="${SCAGENT_SDK_ENVIRONMENTS_FILE:-${_SCAGENT_SDK_ROOT}/configs/environments/iris.toml}"
export SCAGENT_SDK_ENV_FILE="${SCAGENT_SDK_ENV_FILE:-${_SCAGENT_SDK_ROOT}/.env}"

echo "scagent-sdk environment ready"
echo "  agent:    ${VIRTUAL_ENV}/bin/python"
echo "  profile:  ${SCAGENT_SDK_MODEL_PROFILE}"
echo "  sessions: ${SCAGENT_SDK_SESSIONS_DIR}"
echo "  compute:  brokered from ${SCAGENT_SDK_ENVIRONMENTS_FILE}"
echo "  locks:    uv.lock + pixi.lock"
echo "Run OpenCode-backed scAgent: scagent"
echo "Run the native SDK terminal: scagent-sdk start"

unset _SCAGENT_SDK_ROOT _SCAGENT_SDK_UV _SCAGENT_SDK_UV_CANDIDATE
unset _SCAGENT_SDK_PIXI _SCAGENT_SDK_PIXI_CANDIDATE
