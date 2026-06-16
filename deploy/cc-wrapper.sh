# marrow cc-wrapper — cross-channel switch + resume loop.
# Source from ~/.zshrc:  source /path/to/marrow/deploy/cc-wrapper.sh
# Requires: mw CLI (pip install marrow)

_marrow_cc_loop() {
  local base_cwd="$1"; shift
  local marker="$HOME/.config/marrow/next-resume.sid"
  cd "$base_cwd" || return 1
  while true; do
    if [[ -f "$marker" ]]; then
      local sid target_cwd effort
      sid=$(sed -n '1p' "$marker")
      target_cwd=$(sed -n '2p' "$marker")
      effort=$(sed -n '3p' "$marker")
      rm -f "$marker"
      [[ -n "$target_cwd" && -d "$target_cwd" ]] && cd "$target_cwd"
      if [[ -n "$effort" ]]; then
        claude --resume "$sid" --effort "$effort" "$@"
      else
        claude --resume "$sid" "$@"
      fi
    else
      claude "$@"
    fi
    [[ -f "$marker" ]] || break
  done
}

switch() {
  local marker="$HOME/.config/marrow/next-resume.sid"
  local tsv sid8 project tag hhmm epoch
  tsv=$(mw list-recent-sessions --limit 10 2>/dev/null) || { echo "mw failed"; return 1; }
  [[ -z "$tsv" ]] && { echo "(no sessions)"; return 0; }

  local -a sids cwds efforts
  local i=0
  echo "Recent sessions:"
  while IFS=$'\t' read -r sid model channel cwd last_active title effort; do
    ((i++))
    sids+=("$sid")
    cwds+=("$cwd")
    efforts+=("$effort")
    sid8="${sid:0:8}"
    project="${cwd##*/}"
    [[ -n "$project" ]] && tag="[${channel}·${project}]" || tag="[${channel}]"
    [[ -z "$title" || "$title" == "-" ]] && title="(untitled)"
    epoch=$(TZ=UTC date -jf '%Y-%m-%dT%H:%M:%SZ' "$last_active" '+%s' 2>/dev/null)
    if [[ -n "$epoch" ]]; then
      hhmm=$(TZ=Australia/Melbourne date -r "$epoch" '+%H:%M')
    else
      hhmm="${last_active:11:5}"
    fi
    printf "  %2d. %s %s (%s) %s %s\n" "$i" "$tag" "$title" "$sid8" "$model" "$hhmm"
  done <<< "$tsv"

  printf "\nPick (empty to cancel): "
  local pick
  read -r pick
  [[ -z "$pick" || "$pick" != <-> ]] && { echo "(cancelled)"; return 0; }
  ((pick < 1 || pick > i)) && { echo "(out of range)"; return 0; }

  local target_sid="${sids[$pick]}"
  local target_cwd="${cwds[$pick]}"
  local target_effort="${efforts[$pick]}"

  mkdir -p "$(dirname "$marker")"
  printf '%s\n%s\n%s' "$target_sid" "$target_cwd" "$target_effort" > "$marker"
  _marrow_cc_loop "${target_cwd:-$PWD}" "$@"
}

# --- Custom shortcuts (example, adjust paths to your setup) ---
# my_project() { _marrow_cc_loop "$HOME/my-project" "$@" }
