#!/usr/bin/env bash
# =====================================================================
# ZMK Vision — shared Docker stack helpers (sourced by start.sh and
# installers/install-linux.sh).
#
# Why this file exists
# --------------------
# Both launchers used to run `docker compose up -d --build` on EVERY start.
# That re-ran `npm ci`, `apt-get` and `pip` in every image, so a simple
# restart downloaded hundreds of megabytes again and looked like an endless
# "loading" bar (especially on slow/flaky links, where docker retries a stalled
# layer pull for minutes without printing anything).
#
# The helpers below make a start cheap and predictable:
#   * stack_needs_build()  — rebuild only when sources actually changed or an
#     image is missing (fingerprint of every build context);
#   * stack_prepull()      — download the base images once, in parallel;
#   * stack_watchdog()     — hard cap for any docker command, so a stalled
#     pull fails with an explanation instead of hanging forever;
#   * wait_http()          — health probe with visible progress.
#
# Everything is overridable through environment variables:
#   ZMK_BUILD_TIMEOUT   seconds before a build is aborted   (default 2400)
#   ZMK_PULL_TIMEOUT    seconds for one base-image pull     (default 600)
#   ZMK_REBUILD=1       force a full rebuild
#   ZMK_NO_PREPULL=1    skip the parallel pre-pull
#   ZMK_QUIET=1         less chatter
# =====================================================================

ZMK_BUILD_TIMEOUT="${ZMK_BUILD_TIMEOUT:-2400}"
ZMK_PULL_TIMEOUT="${ZMK_PULL_TIMEOUT:-600}"
ZMK_FINGERPRINT_FILE="${ZMK_FINGERPRINT_FILE:-.zmk-build-fingerprint}"

zmk_note(){ [[ "${ZMK_QUIET:-0}" == "1" ]] || echo "$@"; }

# Run a command with a hard time limit when coreutils `timeout` is available.
# A docker pull against an unreachable registry is the classic "it hangs
# forever" case; without this the launcher waits indefinitely and the user
# sees a frozen installer.
stack_watchdog(){
  local seconds="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "$seconds" "$@"
  else
    "$@"
  fi
}

# Print plain build progress. The default "auto" mode hides the step that is
# currently downloading, which is exactly what makes a slow start look hung.
stack_prepare_build_env(){
  export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"
  export BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}"
  export PROGRESS_NO_TRUNC="${PROGRESS_NO_TRUNC:-1}"
  # Fail a stalled HTTP transfer instead of retrying it forever.
  export DOCKER_CLIENT_TIMEOUT="${DOCKER_CLIENT_TIMEOUT:-300}"
  export COMPOSE_HTTP_TIMEOUT="${COMPOSE_HTTP_TIMEOUT:-300}"
  # Keep the classic builder path available: it is the recovery route when
  # BuildKit's cache is corrupted, and it never triggers the bake warning.
  export COMPOSE_BAKE="${COMPOSE_BAKE:-false}"
  export COMPOSE_DOCKER_CLI_BUILD="${COMPOSE_DOCKER_CLI_BUILD:-0}"
}

stack_fingerprint(){
  # Hash the content of every build context, not the mtime: a `git pull` that
  # touches nothing and a plain restart must not trigger a rebuild.
  local files digest
  files=$(find backend frontend services -type f \
      \( -name 'Dockerfile*' -o -name 'requirements*.txt' -o -name 'package.json' \
         -o -name 'package-lock.json' -o -name 'nginx.conf' -o -name '*.py' \
         -o -name '*.ts' -o -name '*.tsx' -o -name '*.css' -o -name '*.html' \
         -o -name '*.yaml' -o -name '*.yml' \) \
      -not -path '*/node_modules/*' -not -path '*/dist/*' 2>/dev/null | LC_ALL=C sort)
  digest=$(printf '%s\n' "${COMPOSE_FILE:-docker-compose.yml}" "${files}" \
    | while IFS= read -r f; do [[ -n "$f" && -f "$f" ]] && sha256sum "$f" 2>/dev/null; done \
    | sha256sum 2>/dev/null | awk '{print $1}')
  printf '%s\n' "${digest:-none}"
}

stack_images(){
  "${DC[@]}" "${PROFILE[@]}" config --images 2>/dev/null | sed 's/[[:space:]]*$//' | sort -u
}

stack_images_present(){
  local img found=0
  while IFS= read -r img; do
    [[ -n "$img" ]] || continue
    found=1
    "${DOCKER[@]}" image inspect "$img" >/dev/null 2>&1 || return 1
  done < <(stack_images)
  [[ "$found" == "1" ]] || return 1
  return 0
}

stack_needs_build(){
  [[ "${ZMK_REBUILD:-0}" == "1" ]] && return 0
  [[ -f "$ZMK_FINGERPRINT_FILE" ]] || return 0
  local current
  current=$(tr -d '[:space:]' < "$ZMK_FINGERPRINT_FILE" 2>/dev/null || true)
  [[ -n "$current" && "$current" == "$(stack_fingerprint)" ]] || return 0
  stack_images_present || return 0
  return 1
}

stack_save_fingerprint(){
  stack_fingerprint > "$ZMK_FINGERPRINT_FILE" 2>/dev/null || true
}

# Download the base images once, concurrently. During a build every service
# pulls its own base layer sequentially, which multiplies a slow link by the
# number of services; pulling them up front turns that into one parallel fetch.
stack_enabled_contexts(){
  # Build contexts of the services that are really enabled for this run, so a
  # profile that is switched off never downloads its (often multi-GB) base
  # image. Falls back to every Dockerfile when compose cannot emit JSON.
  "${DC[@]}" "${PROFILE[@]}" config --format json 2>/dev/null \
    | grep -oE '"context"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*:[[:space:]]*"(.*)"/\1/' | sort -u
}

stack_prepull(){
  [[ "${ZMK_NO_PREPULL:-0}" == "1" ]] && return 0
  local ctx bases="" base pids=() running=0
  while IFS= read -r ctx; do
    [[ -n "$ctx" && -d "$ctx" ]] || continue
    bases+="$(grep -rhoE '^[[:space:]]*FROM[[:space:]]+[^[:space:]]+' "$ctx"/Dockerfile* 2>/dev/null | awk '{print $2}' | grep -vi '^scratch$')
"
  done < <(stack_enabled_contexts)
  bases=$(printf '%s\n' "$bases" | sed '/^$/d' | sort -u)
  [[ -z "$bases" ]] && return 0
  local total
  total=$(printf '%s\n' "$bases" | wc -l | tr -d ' ')
  zmk_note "[start] Предзагружаю базовые образы ($total) параллельно..."
  while IFS= read -r base; do
    [[ -n "$base" ]] || continue
    ( stack_watchdog "$ZMK_PULL_TIMEOUT" "${DOCKER[@]}" pull "$base" >/dev/null 2>&1 ) &
    pids+=($!)
    running=$((running+1))
    # Keep at most 4 concurrent pulls: enough to saturate a normal link
    # without tripping registry rate limits.
    if (( running >= 4 )); then wait "${pids[0]}" 2>/dev/null || true; pids=("${pids[@]:1}"); running=$((running-1)); fi
  done <<< "$bases"
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  [[ "${ZMK_QUIET:-0}" == "1" ]] || echo "[start] Базовые образы готовы."
  return 0
}

# Health probe with visible progress, so waiting never looks like a freeze.
wait_http(){
  local url="$1" seconds="${2:-120}" i=0
  zmk_note "[start] Жду $url (до ${seconds}с)..."
  while ((i < seconds)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      [[ "${ZMK_QUIET:-0}" == "1" ]] || echo " готово"
      return 0
    fi
    printf '.'
    sleep 2
    i=$((i+2))
  done
  echo " нет ответа"
  return 1
}
