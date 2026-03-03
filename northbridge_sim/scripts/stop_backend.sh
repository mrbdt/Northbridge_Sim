#!/usr/bin/env bash
set -euo pipefail

echo "== Stop backend =="

echo "-- Kill anything listening on :8000"
PIDS="$(lsof -ti tcp:8000 || true)"
if [[ -n "${PIDS}" ]]; then
  echo "Killing listeners: ${PIDS}"
  kill ${PIDS} || true
  sleep 0.5
  kill -9 ${PIDS} 2>/dev/null || true
else
  echo "No LISTEN process on :8000"
fi

echo "-- Kill uvicorn/backend.main processes (if any)"
PIDS2="$(ps aux | grep -E 'python.*uvicorn|uvicorn|backend\.main' | grep -v grep | awk '{print $2}' | tr '\n' ' ')"
if [[ -n "${PIDS2// /}" ]]; then
  echo "Killing: ${PIDS2}"
  kill ${PIDS2} || true
  sleep 0.5
  kill -9 ${PIDS2} 2>/dev/null || true
else
  echo "No uvicorn/backend.main processes found"
fi

echo "-- Optional: kill orphan multiprocessing helpers (spawn_main/resource_tracker)"
PIDS3="$(ps aux | grep -E 'multiprocessing\.spawn|multiprocessing\.resource_tracker' | grep -v grep | awk '{print $2}' | tr '\n' ' ')"
if [[ -n "${PIDS3// /}" ]]; then
  echo "Killing orphans: ${PIDS3}"
  kill ${PIDS3} || true
  sleep 0.2
  kill -9 ${PIDS3} 2>/dev/null || true
else
  echo "No multiprocessing orphans found"
fi

echo "Done."