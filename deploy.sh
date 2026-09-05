#!/usr/bin/env bash
# One-command VPS deployment. Configuration: .deploy.conf (see example).
set -Eeuo pipefail

main() {
  local script_dir repo_dir git_dir api_service cloudflare_service python_bin
  local local_health_url public_health_url health_timeout service
  local old_revision new_revision stage="preflight"
  local -a elevated=()

  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  cd "$script_dir"
  repo_dir="$(git rev-parse --show-toplevel)"
  [[ "$repo_dir" == "$script_dir" ]] || { echo "Run this script from the backend repository root." >&2; return 1; }

  # This optional file is trusted shell configuration owned by the deployer.
  if [[ -f "$repo_dir/.deploy.conf" ]]; then source "$repo_dir/.deploy.conf"; fi
  api_service="${API_SERVICE:-bok-api.service}"
  cloudflare_service="${CLOUDFLARED_SERVICE:-cloudflared.service}"
  python_bin="${PYTHON_BIN:-$repo_dir/.venv/bin/python}"
  local_health_url="${LOCAL_HEALTH_URL:-http://127.0.0.1:8000/health}"
  public_health_url="${PUBLIC_HEALTH_URL:-https://api.bokzgacilo.com/health}"
  health_timeout="${HEALTH_TIMEOUT:-180}"
  [[ "$health_timeout" =~ ^[1-9][0-9]*$ ]] || { echo "HEALTH_TIMEOUT must be a positive integer." >&2; return 1; }

  for command in git systemctl curl flock; do
    command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; return 1; }
  done
  if (( EUID != 0 )); then
    command -v sudo >/dev/null || { echo "sudo is required to restart services." >&2; return 1; }
    sudo -v
    elevated=(sudo)
  fi

  git_dir="$(git rev-parse --absolute-git-dir)"
  exec 9>"$git_dir/backend-deploy.lock"
  flock -n 9 || { echo "Another backend deployment is already running." >&2; return 1; }

  # Do not overwrite edits on the VPS. Git itself checks untracked-file conflicts.
  git diff --quiet && git diff --cached --quiet || {
    echo "Backend has uncommitted tracked changes. Commit or stash them before deploying." >&2
    return 1
  }
  git symbolic-ref --quiet --short HEAD >/dev/null || { echo "Detached HEAD: check out the deployment branch first." >&2; return 1; }
  git rev-parse --abbrev-ref '@{upstream}' >/dev/null || { echo "Set an upstream for the deployment branch first." >&2; return 1; }

  [[ "$api_service" == *.service ]] || api_service="${api_service}.service"
  [[ "$cloudflare_service" == *.service ]] || cloudflare_service="${cloudflare_service}.service"
  for service in "$api_service" "$cloudflare_service"; do
    [[ "$service" =~ ^[a-zA-Z0-9_.@:-]+\.service$ ]] || { echo "Invalid service name." >&2; return 1; }
    [[ "$(systemctl show "$service" --property=LoadState --value)" == "loaded" ]] || {
      echo "Service not installed: $service" >&2; return 1;
    }
  done
  [[ "$api_service" != "$cloudflare_service" ]] || { echo "Backend and tunnel must use different services." >&2; return 1; }
  if [[ ! -x "$python_bin" && -z "${PYTHON_BIN:-}" && -x "$repo_dir/venv/bin/python" ]]; then
    python_bin="$repo_dir/venv/bin/python"
  fi
  [[ -x "$python_bin" ]] || { echo "Python not found: $python_bin. Set PYTHON_BIN to the service's virtualenv Python in .deploy.conf." >&2; return 1; }
  "$python_bin" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else "Use the backend virtualenv Python, not system Python.")'

  # Diagnostics deliberately omit service command lines, which may contain a tunnel token.
  trap 'echo "Deployment failed during: $stage. No automatic rollback was performed." >&2; echo "Inspect: sudo journalctl -u $api_service -n 80 --no-pager" >&2; echo "Inspect: sudo journalctl -u $cloudflare_service -n 80 --no-pager" >&2' ERR

  old_revision="$(git rev-parse --short HEAD)"
  stage="git pull"
  echo "[1/5] Pulling the current branch from its configured upstream..."
  git -c pull.rebase=false pull --ff-only
  new_revision="$(git rev-parse --short HEAD)"

  stage="dependency installation"
  echo "[2/5] Installing backend dependencies..."
  "$python_bin" -m pip install -r "$repo_dir/requirements.txt"
  "$python_bin" -m pip check
  "$python_bin" -m py_compile "$repo_dir/main.py"

  stage="backend restart and local health check"
  echo "[3/5] Restarting $api_service..."
  "${elevated[@]}" systemctl restart "$api_service"
  "${elevated[@]}" systemctl is-active --quiet "$api_service"
  wait_for_health "$local_health_url" "$python_bin" "$health_timeout"

  stage="Cloudflare tunnel restart"
  echo "[4/5] Restarting $cloudflare_service..."
  "${elevated[@]}" systemctl restart "$cloudflare_service"
  "${elevated[@]}" systemctl is-active --quiet "$cloudflare_service"

  stage="public health check"
  echo "[5/5] Checking $public_health_url..."
  wait_for_health "$public_health_url" "$python_bin" "$health_timeout"
  echo "Deployment complete: $old_revision -> $new_revision. Backend and public API are ready."
  trap - ERR
}

wait_for_health() {
  local url="$1" python_bin="$2" timeout="$3" deadline=$((SECONDS + $3))
  while (( SECONDS < deadline )); do
    if curl --fail --silent --connect-timeout 3 --max-time 5 "$url" |
      "$python_bin" -c 'import json,sys; data=json.load(sys.stdin); sys.exit(0 if data.get("status") == "ok" and data.get("ready") is True else 1)' 2>/dev/null; then
      echo "Ready: $url"
      return 0
    fi
    sleep 2
  done
  echo "Health check failed after approximately ${timeout}s: $url" >&2
  return 1
}

# Keep the deployment body in a function so pulling an updated copy of this
# script cannot change the commands halfway through this invocation.
main "$@"
