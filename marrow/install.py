"""python -m marrow install — one-command setup / re-sync for marrow."""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]       # marrow/ repo root
_DEPLOY_DIR = _REPO_ROOT / "deploy"
_VENV = _REPO_ROOT / ".venv"
_VENV_PYTHON = _VENV / "bin" / "python"
_CONFIG_DIR = Path.home() / ".config" / "marrow"
_CLAUDE_DIR = Path.home() / ".claude"
_SETTINGS = _CLAUDE_DIR / "settings.json"
_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
_PATH_ENV = (
    f"{Path.home() / '.local' / 'bin'}:"
    f"{_VENV / 'bin'}:"
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
)

# Hook definitions — {venv} replaced at write time
_MARROW_HOOKS: dict[str, list[dict]] = {
    "PreToolUse": [
        {"matcher": "Write",  "command": "{venv} -m marrow.hooks pretool_use"},
        {"matcher": "Edit",   "command": "{venv} -m marrow.hooks pretool_use"},
        {"matcher": "Bash",   "command": "{venv} -m marrow.hooks pretool_use"},
    ],
    "SessionStart": [
        {"matcher": "", "command": "{venv} -m marrow.hooks session_start"},
    ],
    "SessionEnd": [
        {"matcher": "", "command": "{venv} -m marrow.hooks session_end"},
    ],
    "UserPromptSubmit": [
        {"matcher": "", "command": "{venv} -m marrow.hooks user_prompt_submit"},
    ],
}

_ALL_PLISTS: list[tuple[str, str]] = [
    ("mw-aging.plist",          "com.marrow.aging"),
    ("mw-daily-catchup.plist",  "com.marrow.daily-catchup"),
    ("mw-daily-routine.plist",  "com.marrow.daily-routine"),
    ("mw-dashboard-tick.plist", "com.marrow.dashboard-tick"),
    ("mw-db-backup.plist",      "com.marrow.db-backup"),
    ("mw-watcher.plist",        "com.marrow.watcher"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(msg: str) -> None:  print(f"  ✓ {msg}")
def _act(msg: str) -> None: print(f"  → {msg}")
def _fail(msg: str) -> None: print(f"  ✗ {msg}", file=sys.stderr)


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=check)


def _launchctl(*args: str) -> tuple[int, str]:
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _uid() -> str:
    return _run("id", "-u").stdout.strip()


# ---------------------------------------------------------------------------
# Step 1: Prerequisites
# ---------------------------------------------------------------------------

def check_prereqs() -> bool:
    ok = True
    if platform.system() != "Darwin":
        _fail(f"macOS required (detected: {platform.system()})")
        ok = False
    if sys.version_info < (3, 12):
        _fail(f"Python >= 3.12 required (got {sys.version_info.major}.{sys.version_info.minor})")
        ok = False
    if not shutil.which("claude"):
        _fail("claude CLI not found on PATH")
        ok = False
    if ok:
        _ok("prerequisites passed")
    return ok


# ---------------------------------------------------------------------------
# Step 2: Venv + editable install
# ---------------------------------------------------------------------------

def setup_venv() -> bool:
    if not _VENV.exists():
        _act("creating .venv")
        r = subprocess.run([sys.executable, "-m", "venv", str(_VENV)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _fail(f"venv creation failed: {r.stderr.strip()}")
            return False
        _ok(".venv created")
    else:
        _ok(".venv already exists")

    _act("pip install -e .")
    r = subprocess.run([str(_VENV_PYTHON), "-m", "pip", "install", "-e", str(_REPO_ROOT)],
                       capture_output=True, text=True, cwd=str(_REPO_ROOT))
    if r.returncode != 0:
        _fail(f"pip install failed: {r.stderr.strip()[-300:]}")
        return False
    _ok("marrow installed in venv")
    return True


# ---------------------------------------------------------------------------
# Step 3: Config + DB
# ---------------------------------------------------------------------------

def setup_config() -> bool:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (_CONFIG_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (_CONFIG_DIR / "db-pages").mkdir(parents=True, exist_ok=True)
    paths_cfg = _CONFIG_DIR / "paths.toml"
    if not paths_cfg.exists():
        paths_cfg.write_text(
            'marrow_db = "~/.config/marrow/marrow.db"\n'
            'ny_root = "~/.config/marrow"\n'
            'dashboard_md = "~/.config/marrow/dashboard.md"\n'
            'drift_pending_dir = "~/.config/marrow/drift_pending"\n'
            'drift_backup_dir = "~/.config/marrow/drift_backup"\n'
            'dir_tree_md = "~/.config/marrow/dir_tree.md"\n'
            'logs_dir = "~/.config/marrow/logs"\n'
            'state_dir = "~/.config/marrow/state"\n',
            encoding="utf-8",
        )
        _ok("paths.toml written")
    cfg = _CONFIG_DIR / "config.toml"
    if not cfg.exists():
        src = _REPO_ROOT / "marrow" / "config.default.toml"
        shutil.copy(src, cfg)
        cfg_text = cfg.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(
            'dashboard = ""',
            f'dashboard = "{(_CONFIG_DIR / "dashboard.md").as_posix()}"',
            1,
        )
        cfg_text = cfg_text.replace(
            'db_pages = "~/.config/marrow/db-pages"',
            f'db_pages = "{(_CONFIG_DIR / "db-pages").as_posix()}"',
            1,
        )
        cfg.write_text(cfg_text, encoding="utf-8")
        _act(f"config written to {cfg}")
        print("    Optional: edit ~/.config/marrow/config.toml to set your persona")
    else:
        _ok("config.toml already exists")

    db = _CONFIG_DIR / "marrow.db"
    if not db.exists():
        _act("initialising marrow.db")
        code = "from marrow import storage; c = storage.init_db(); c.close()"
        r = subprocess.run([str(_VENV_PYTHON), "-c", code],
                           capture_output=True, text=True, cwd=str(_REPO_ROOT))
        if r.returncode != 0:
            _fail(f"DB init failed: {(r.stderr or r.stdout).strip()[-500:]}")
            return False
        _ok("marrow.db initialised")
    else:
        _ok("marrow.db already exists")
    return True


def render_initial_surface() -> bool:
    _act("rendering dashboard + sub-pages")
    r = subprocess.run([str(_VENV_PYTHON), "-m", "marrow.cli", "refresh", "--all"],
                       capture_output=True, text=True, cwd=str(_REPO_ROOT))
    if r.returncode != 0:
        _fail(f"initial render failed: {(r.stderr or r.stdout).strip()[-500:]}")
        return False
    _ok((r.stdout.strip() or "dashboard rendered").splitlines()[-1])
    return True


# ---------------------------------------------------------------------------
# Step 4: Hooks in settings.json
# ---------------------------------------------------------------------------

def _hook_command(template: str) -> str:
    return template.replace("{venv}", str(_VENV_PYTHON))


def _is_marrow_hook(cmd: str) -> bool:
    return "marrow.hooks" in cmd and str(_REPO_ROOT) in cmd or \
           "marrow.hooks" in cmd and str(_VENV_PYTHON) in cmd


def register_hooks() -> bool:
    _SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if _SETTINGS.exists():
        try:
            settings: dict = json.loads(_SETTINGS.read_text())
        except json.JSONDecodeError:
            _fail(f"settings.json is invalid JSON — fix manually: {_SETTINGS}")
            return False
    else:
        settings = {}

    hooks: dict = settings.setdefault("hooks", {})

    for event, entries in _MARROW_HOOKS.items():
        event_list: list = hooks.setdefault(event, [])

        for entry in entries:
            new_cmd = _hook_command(entry["command"])
            matcher = entry["matcher"]

            # Find existing group with this matcher
            group = next(
                (g for g in event_list if g.get("matcher") == matcher),
                None,
            )
            if group is None:
                group = {"matcher": matcher, "hooks": []}
                event_list.append(group)

            group_hooks: list = group.setdefault("hooks", [])

            # Remove stale marrow hooks for this matcher (path may have changed)
            group_hooks[:] = [
                h for h in group_hooks
                if not ("marrow.hooks" in h.get("command", ""))
            ]
            group_hooks.append({"type": "command", "command": new_cmd})

    _SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    _ok("hooks registered in settings.json")
    return True


# ---------------------------------------------------------------------------
# Step 5: MCP server
# ---------------------------------------------------------------------------

def register_mcp() -> bool:
    r = subprocess.run(
        ["claude", "mcp", "add", "marrow", "-s", "user",
         "--", str(_VENV_PYTHON), "-m", "marrow.daemon"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        msg = (r.stdout + r.stderr).strip()
        if "already" not in msg.lower():
            _fail(f"mcp add failed: {msg}")
            return False
        _act("MCP server exists; replacing stale registration")
        subprocess.run(["claude", "mcp", "remove", "marrow", "-s", "user"],
                       capture_output=True, text=True)
        r = subprocess.run(
            ["claude", "mcp", "add", "marrow", "-s", "user",
             "--", str(_VENV_PYTHON), "-m", "marrow.daemon"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            _fail(f"mcp replace failed: {(r.stdout + r.stderr).strip()}")
            return False
    _ok("MCP server registered (marrow)")

    # Ensure mcp__marrow__* in permissions.allow
    if _SETTINGS.exists():
        settings = json.loads(_SETTINGS.read_text())
        perms: dict = settings.setdefault("permissions", {})
        allow: list = perms.setdefault("allow", [])
        marker = "mcp__marrow__"
        if not any(marker in str(a) for a in allow):
            allow.append("mcp__marrow__*")
            _SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
            _ok("mcp__marrow__* added to permissions.allow")
    return True


# ---------------------------------------------------------------------------
# Step 6: Symlink commands + agents
# ---------------------------------------------------------------------------

def _sync_symlinks(src_dir: Path, dst_dir: Path) -> bool:
    if not src_dir.exists():
        _ok(f"{src_dir.name}/ not found in deploy/ — skipping")
        return True
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.glob("*.md"):
        dst = dst_dir / src.name
        if dst.exists() and not dst.is_symlink():
            bak = dst.with_suffix(dst.suffix + ".bak")
            dst.rename(bak)
            _act(f"backed up existing {dst.name} → {dst.name}.bak")
        if dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)
        _ok(f"symlinked {dst_dir.name}/{src.name}")
    return True


def sync_symlinks() -> bool:
    ok = _sync_symlinks(_DEPLOY_DIR / "commands", _CLAUDE_DIR / "commands")
    ok = _sync_symlinks(_DEPLOY_DIR / "agents",   _CLAUDE_DIR / "agents") and ok
    return ok


# ---------------------------------------------------------------------------
# Step 7: launchd plists
# ---------------------------------------------------------------------------

def _resolve_plist(text: str) -> str:
    log_dir = _CONFIG_DIR / "logs"
    return (
        text
        .replace("__VENV_PYTHON__",  str(_VENV_PYTHON))
        .replace("__PROJECT_DIR__",  str(_REPO_ROOT))
        .replace("__LOG_DIR__",      str(log_dir))
        .replace("__DATA_DIR__",     str(_CONFIG_DIR))
        .replace("__PATH_ENV__",     _PATH_ENV)
    )


def install_plists() -> bool:
    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    (_CONFIG_DIR / "logs").mkdir(parents=True, exist_ok=True)
    uid = _uid()
    domain = f"gui/{uid}"
    errors = 0
    for fname, label in _ALL_PLISTS:
        src = _DEPLOY_DIR / fname
        if not src.exists():
            _act(f"[skip] {fname} not in deploy/")
            continue
        resolved = _resolve_plist(src.read_text())
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        tgt.write_text(resolved)
        _launchctl("bootout", domain, str(tgt))   # tolerated
        rc, msg = _launchctl("bootstrap", domain, str(tgt))
        if rc == 0:
            _ok(f"{label} loaded")
        else:
            _fail(f"{label}: {msg}")
            errors += 1
    return errors == 0


# ---------------------------------------------------------------------------
# Uninstall helpers
# ---------------------------------------------------------------------------

def remove_hooks() -> None:
    if not _SETTINGS.exists():
        return
    try:
        settings = json.loads(_SETTINGS.read_text())
    except json.JSONDecodeError:
        _fail("settings.json is invalid JSON — skipping hook removal")
        return
    hooks: dict = settings.get("hooks", {})
    for event, event_list in hooks.items():
        for group in event_list:
            gh: list = group.get("hooks", [])
            before = len(gh)
            gh[:] = [h for h in gh if "marrow.hooks" not in h.get("command", "")]
            if len(gh) < before:
                _ok(f"removed marrow hooks from {event}/{group.get('matcher','')!r}")
    # prune empty groups
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    _SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")


def remove_mcp() -> None:
    r = subprocess.run(["claude", "mcp", "remove", "marrow", "-s", "user"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        _ok("MCP server removed")
    else:
        _act(f"mcp remove: {(r.stdout + r.stderr).strip()}")


def remove_symlinks() -> None:
    for subdir in ("commands", "agents"):
        d = _CLAUDE_DIR / subdir
        if not d.exists():
            continue
        for link in d.iterdir():
            if link.is_symlink():
                target = link.resolve()
                if str(_REPO_ROOT) in str(target):
                    link.unlink()
                    _ok(f"removed symlink {subdir}/{link.name}")


def remove_plists() -> None:
    uid = _uid()
    domain = f"gui/{uid}"
    for _fname, label in _ALL_PLISTS:
        tgt = _LAUNCH_AGENTS / f"{label}.plist"
        if not tgt.exists():
            continue
        _launchctl("bootout", domain, str(tgt))
        tgt.unlink(missing_ok=True)
        _ok(f"removed {label}")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_install(update: bool = False) -> int:
    print(f"marrow install {'(--update)' if update else ''}")
    print(f"  repo: {_REPO_ROOT}")
    print()

    if not check_prereqs():
        return 1

    if not update:
        print("\n[2] venv")
        if not setup_venv():
            return 1

        print("\n[3] config + DB")
        if not setup_config():
            return 1
        if not render_initial_surface():
            return 1

    print("\n[4] hooks")
    if not register_hooks():
        return 1

    print("\n[5] MCP")
    if not register_mcp():
        return 1

    print("\n[6] commands + agents")
    if not sync_symlinks():
        return 1

    print("\n[7] launchd plists")
    if not install_plists():
        return 1

    print("\n✓ marrow install complete")
    return 0


def run_uninstall() -> int:
    print("marrow uninstall")
    remove_hooks()
    remove_mcp()
    remove_symlinks()
    remove_plists()
    print("\n✓ uninstall complete")
    return 0
