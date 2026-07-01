#!/usr/bin/env python3
"""mac-cleaner — explore disk usage and clean caches on macOS.

A dependency-free (stdlib only) CLI to help you find what is eating your disk
and safely reclaim space.

Commands
    scan      Show a folder's disk usage, level by level (sorted, biggest first)
    explore   Interactively drill into folders and delete from there
    suggest   Scan known cache locations and report how much you could reclaim
    clean     Delete known-safe caches (browser / Xcode / dev tools), with confirm

Nothing is deleted without an explicit confirmation. `clean` defaults to a
dry-run preview; you must pass --apply (or answer the prompt) to remove anything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass

HOME = os.path.expanduser("~")

# ---------------------------------------------------------------------------
# Output helpers (color + sizes)
# ---------------------------------------------------------------------------

_USE_COLOR = ((sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR")))
              and os.environ.get("NO_COLOR") is None)


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def red(t: str) -> str:
    return _c("31", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def blue(t: str) -> str:
    return _c("34", t)


def cyan(t: str) -> str:
    return _c("36", t)


def human(n: int) -> str:
    """Human-readable byte size (binary units, e.g. 3.4G)."""
    step = 1024.0
    units = ["B", "K", "M", "G", "T", "P"]
    size = float(n)
    for u in units:
        if size < step or u == units[-1]:
            if u == "B":
                return f"{int(size)}{u}"
            return f"{size:.1f}{u}"
        size /= step
    return f"{size:.1f}P"


def status(msg: str) -> None:
    """Transient one-line status printed to stderr (only when interactive)."""
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K" + msg)
        sys.stderr.flush()


def clear_status() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Disk usage measurement (uses allocated blocks = real space freed)
# ---------------------------------------------------------------------------

def entry_alloc(st) -> int:
    """Real bytes a file occupies on disk (512-byte blocks)."""
    # st_blocks is the number of 512-byte blocks actually allocated.
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None:
        return blocks * 512
    return st.st_size


_scan_counter = 0


def tree_size(path: str, show_progress: bool = False) -> int:
    """Recursively compute allocated size of a path, ignoring errors."""
    global _scan_counter
    total = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return entry_alloc(st)

    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += entry_alloc(st)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        if show_progress:
                            _scan_counter += 1
                            if _scan_counter % 2000 == 0:
                                status(f"  scanning… {human(total)}")
        except OSError:
            continue
    return total


@dataclass
class Child:
    name: str
    path: str
    size: int
    is_dir: bool


def list_children(path: str, show_progress: bool = False) -> list[Child]:
    """Return immediate children of `path` with their recursive sizes, sorted."""
    children: list[Child] = []
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        raise e
    for entry in entries:
        is_dir = entry.is_dir(follow_symlinks=False)
        if show_progress:
            status(f"  sizing {entry.name[:40]}…")
        size = tree_size(entry.path, show_progress=show_progress)
        children.append(Child(entry.name, entry.path, size, is_dir))
    clear_status()
    children.sort(key=lambda c: c.size, reverse=True)
    return children


# ---------------------------------------------------------------------------
# Registry of known-cleanable locations
# ---------------------------------------------------------------------------

# safety levels:
#   safe    -> regenerated automatically, no meaningful data loss
#   caution -> safe-ish, but may need re-download / slower first run / lose state
#   risky   -> could lose real work; never auto-cleaned, only reported

@dataclass
class Target:
    name: str
    category: str          # browser | xcode | dev | system
    safety: str            # safe | caution | risky
    patterns: list[str]    # glob patterns (under ~ unless absolute)
    note: str
    contents_only: bool = True  # empty the dir but keep the dir itself


def H(*parts: str) -> str:
    return os.path.join(HOME, *parts)


REGISTRY: list[Target] = [
    # ---- Browsers (cache only — history/passwords/bookmarks untouched) ----
    Target("Safari cache", "browser", "safe",
           ["Library/Caches/com.apple.Safari", "Library/Caches/com.apple.WebKit.*"],
           "Web page cache; rebuilt as you browse."),
    Target("Chrome cache", "browser", "safe",
           ["Library/Caches/Google/Chrome",
            "Library/Application Support/Google/Chrome/*/Cache",
            "Library/Application Support/Google/Chrome/*/Code Cache",
            "Library/Application Support/Google/Chrome/*/GPUCache",
            "Library/Application Support/Google/Chrome/*/Service Worker/CacheStorage"],
           "Browser cache only; logins & bookmarks are kept."),
    Target("Edge cache", "browser", "safe",
           ["Library/Caches/Microsoft Edge",
            "Library/Application Support/Microsoft Edge/*/Cache",
            "Library/Application Support/Microsoft Edge/*/Code Cache",
            "Library/Application Support/Microsoft Edge/*/GPUCache"],
           "Browser cache only."),
    Target("Brave cache", "browser", "safe",
           ["Library/Caches/BraveSoftware",
            "Library/Application Support/BraveSoftware/Brave-Browser/*/Cache",
            "Library/Application Support/BraveSoftware/Brave-Browser/*/Code Cache"],
           "Browser cache only."),
    Target("Firefox cache", "browser", "safe",
           ["Library/Caches/Firefox",
            "Library/Application Support/Firefox/Profiles/*/cache2"],
           "Browser cache only."),
    Target("Arc cache", "browser", "safe",
           ["Library/Caches/company.thebrowser.Browser"],
           "Browser cache only."),

    # ---- Xcode / Apple developer ----
    Target("Xcode DerivedData", "xcode", "safe",
           ["Library/Developer/Xcode/DerivedData"],
           "Build intermediates & indexes; Xcode rebuilds them. Usually huge."),
    Target("Xcode build cache", "xcode", "safe",
           ["Library/Caches/com.apple.dt.Xcode",
            "Library/Developer/Xcode/Products"],
           "Xcode's own cache."),
    Target("Swift Package cache", "xcode", "safe",
           ["Library/Caches/org.swift.swiftpm",
            "Library/org.swift.swiftpm"],
           "Swift Package Manager cache; re-downloaded on next build."),
    Target("CoreSimulator caches", "xcode", "safe",
           ["Library/Developer/CoreSimulator/Caches"],
           "Simulator runtime caches; regenerated."),
    Target("Xcode DeviceSupport", "xcode", "caution",
           ["Library/Developer/Xcode/iOS DeviceSupport",
            "Library/Developer/Xcode/watchOS DeviceSupport",
            "Library/Developer/Xcode/tvOS DeviceSupport"],
           "Symbols for devices you've debugged; regenerated when you reconnect."),
    Target("Xcode Archives", "xcode", "risky",
           ["Library/Developer/Xcode/Archives"],
           "Your shippable/notarized app builds — deleting loses them. Review first!"),

    # ---- Dev tool caches ----
    Target("npm cache", "dev", "safe",
           [".npm/_cacache"], "npm download cache; re-downloaded as needed."),
    Target("Yarn cache", "dev", "safe",
           ["Library/Caches/Yarn", ".yarn/cache"], "Yarn package cache."),
    Target("pnpm store", "dev", "caution",
           ["Library/pnpm/store"], "Shared pnpm content store; re-fetched on install."),
    Target("pip cache", "dev", "safe",
           ["Library/Caches/pip"], "Python pip wheel/download cache."),
    Target("Homebrew cache", "dev", "safe",
           ["Library/Caches/Homebrew"], "Downloaded bottles; `brew` re-downloads."),
    Target("CocoaPods cache", "dev", "safe",
           ["Library/Caches/CocoaPods", ".cocoapods/repos"],
           "Pod download cache."),
    Target("Go build cache", "dev", "safe",
           ["Library/Caches/go-build"], "Go compiler cache; rebuilt on next build."),
    Target("Go module cache", "dev", "caution",
           ["go/pkg/mod"], "Downloaded Go modules; re-fetched when building."),
    Target("Gradle caches", "dev", "caution",
           [".gradle/caches"], "Gradle dependency cache; re-downloaded."),
    Target("Cargo registry cache", "dev", "caution",
           [".cargo/registry/cache", ".cargo/registry/src"],
           "Rust crate cache; re-downloaded on build."),
    Target("Carthage cache", "dev", "safe",
           ["Library/Caches/org.carthage.CarthageKit"], "Carthage download cache."),

    # ---- System / misc ----
    Target("Trash", "system", "caution",
           [".Trash"], "Items you already moved to Trash. Emptying is permanent."),
    Target("User logs", "system", "caution",
           ["Library/Logs"], "Application logs; safe to clear."),
    Target("Termius session logs", "system", "caution",
           ["Library/Application Support/Termius/session-logs"],
           "Recorded SSH/terminal session transcripts; not needed by the app. "
           "Can grow to hundreds of GB."),
    # NOTE: we deliberately do NOT register all of ~/Library/Caches as a single
    # clean target. Some apps store real data (profiles, sessions) there, and a
    # blanket wipe could log you out or lose state. Unknown items under Caches
    # are flagged as "likely cache" in `explore`/`scan` so you can decide.
]


def expand_target(t: Target) -> list[str]:
    """Return existing absolute paths matching a target's patterns."""
    import glob

    found: list[str] = []
    for pat in t.patterns:
        full = pat if os.path.isabs(pat) else H(pat)
        for p in glob.glob(full):
            if os.path.exists(p):
                found.append(os.path.realpath(p))
    # dedupe, drop nested duplicates
    found = sorted(set(found))
    return found


# ---------------------------------------------------------------------------
# Cleanability classification for the explorer
# ---------------------------------------------------------------------------

_classify_cache: dict[str, Target] | None = None


def _cleanable_index() -> dict[str, Target]:
    global _classify_cache
    if _classify_cache is None:
        idx: dict[str, Target] = {}
        for t in REGISTRY:
            for p in expand_target(t):
                idx[p] = t
        _classify_cache = idx
    return _classify_cache


def classify(path: str) -> tuple[str, Target | None]:
    """Return ('exact'|'inside'|'contains'|'likely'|'none', target)."""
    rp = os.path.realpath(path)
    idx = _cleanable_index()
    if rp in idx:
        return "exact", idx[rp]
    contains = None
    for target_path, t in idx.items():
        if rp.startswith(target_path + os.sep):
            return "inside", t
        if target_path.startswith(rp + os.sep):
            contains = t
    if contains is not None:
        return "contains", contains
    # Heuristic: anything living inside a "Caches" directory is probably
    # disposable, but we don't auto-clean it — just hint.
    if (os.sep + "Caches" + os.sep) in rp or rp.endswith(os.sep + "Caches"):
        return "likely", None
    return "none", None


SAFETY_TAG = {
    "safe": green("● cleanable"),
    "caution": yellow("● cleanable (caution)"),
    "risky": red("● keep — review"),
}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def print_disk_summary() -> None:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return
    pct = usage.used / usage.total * 100
    bar_len = 30
    filled = int(bar_len * usage.used / usage.total)
    bar = "█" * filled + "░" * (bar_len - filled)
    color = red if pct > 90 else (yellow if pct > 75 else green)
    print(f"  Disk /  [{color(bar)}]  "
          f"{human(usage.used)} used / {human(usage.total)}  "
          f"({color(f'{pct:.0f}%')}, {human(usage.free)} free)")
    print()


def disk_summary_line() -> str:
    """Single-line disk summary (used by the interactive TUI)."""
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return ""
    pct = usage.used / usage.total * 100
    bar_len = 20
    filled = int(bar_len * usage.used / usage.total)
    bar = "█" * filled + "░" * (bar_len - filled)
    color = red if pct > 90 else (yellow if pct > 75 else green)
    return (f"Disk / [{color(bar)}] {human(usage.free)} free / "
            f"{human(usage.total)} ({color(f'{pct:.0f}% used')})")


def cmd_scan(args) -> int:
    root = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        print(red(f"Not a directory: {root}"), file=sys.stderr)
        return 1

    print()
    print_disk_summary()
    print(bold(f"  {root}"))

    min_size = parse_size(args.min_size) if args.min_size else 0

    def walk(path: str, depth: int, prefix: str) -> None:
        if depth > args.depth:
            return
        try:
            children = list_children(path, show_progress=True)
        except OSError:
            print(prefix + dim("(permission denied)"))
            return
        children = [c for c in children if c.size >= min_size]
        if args.top:
            children = children[: args.top]
        total = max(1, sum(c.size for c in children))
        for i, c in enumerate(children):
            last = i == len(children) - 1
            branch = "└─ " if last else "├─ "
            kind, t = classify(c.path)
            tag = ""
            if kind in ("exact", "inside") and t:
                tag = "  " + SAFETY_TAG[t.safety]
            elif kind == "contains":
                tag = "  " + dim("· contains caches")
            elif kind == "likely":
                tag = "  " + dim("· likely cache")
            pct = c.size / total * 100
            name = (cyan(c.name) if c.is_dir else c.name)
            size_str = bold(f"{human(c.size):>8}")
            print(f"{prefix}{branch}{size_str}  {name}{tag}  {dim(f'{pct:.0f}%')}")
            if c.is_dir and depth < args.depth:
                ext = "   " if last else "│  "
                walk(c.path, depth + 1, prefix + ext)

    walk(root, 1, "  ")
    clear_status()
    print()
    return 0


# ---- Finder-style interactive TUI (arrow-key navigation, no deps) ----

SAFETY_PLAIN = {
    "safe": "● cleanable",
    "caution": "● cleanable (caution)",
    "risky": "● keep — review",
}
SAFETY_COLOR = {"safe": green, "caution": yellow, "risky": red}


def _w(s: str) -> None:
    sys.stdout.write(s)


def _flush() -> None:
    sys.stdout.flush()


def _read_key(fd: int) -> str:
    """Read a single keypress, decoding arrow-key escape sequences."""
    import select

    ch = os.read(fd, 1)
    if ch == b"\x1b":  # ESC — maybe the start of an arrow sequence
        r, _, _ = select.select([fd], [], [], 0.02)
        if not r:
            return "ESC"
        seq = os.read(fd, 2)
        return {
            b"[A": "UP", b"[B": "DOWN", b"[C": "RIGHT", b"[D": "LEFT",
            b"[H": "HOME", b"[F": "END",
        }.get(seq, "ESC")
    if ch in (b"\r", b"\n"):
        return "ENTER"
    if ch == b"\x03":
        return "CTRL_C"
    if ch == b"\x7f":
        return "BACKSPACE"
    return ch.decode("utf-8", "ignore")


def _shorten(s: str, width: int) -> str:
    if width <= 1 or len(s) <= width:
        return s
    return "…" + s[-(width - 1):]


def _tag_text(path: str) -> tuple[str | None, str]:
    ck, ct = classify(path)
    if ck in ("exact", "inside") and ct:
        return ct.safety, SAFETY_PLAIN[ct.safety]
    if ck == "contains":
        return None, "· contains caches"
    if ck == "likely":
        return None, "· likely cache"
    return None, ""


def _row(c: Child, total: int, selected: bool, cols: int) -> str:
    marker = "❯ " if selected else "  "
    size = f"{human(c.size):>8}"
    barlen = 12
    fill = int(barlen * c.size / total) if total else 0
    bar = "▰" * fill + "▱" * (barlen - fill)
    safety, tag = _tag_text(c.path)
    name = c.name + ("/" if c.is_dir else "")
    fixed = 1 + 2 + 8 + 1 + barlen + 1 + 1  # lead + marker + size + sp + bar + sp + sp
    tagspace = (len(tag) + 1) if tag else 0
    avail = max(8, cols - fixed - tagspace)
    if len(name) > avail:
        name = name[: avail - 1] + "…"
    name_field = name.ljust(avail)
    if selected:
        body = f"{marker}{size} {bar} {name_field} {tag}".rstrip().ljust(cols - 1)
        return " \033[7m" + body + "\033[0m"
    csize = bold(size)
    cbar = dim(bar)
    cname = cyan(name_field) if c.is_dir else name_field
    ctag = SAFETY_COLOR[safety](tag) if safety else dim(tag)
    return f" {marker}{csize} {cbar} {cname} {ctag}".rstrip()


def cmd_explore(args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(red("explore needs an interactive terminal. Try `scan` instead."),
              file=sys.stderr)
        return 1
    import subprocess
    import termios
    import tty

    start = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isdir(start):
        print(red(f"Not a directory: {start}"), file=sys.stderr)
        return 1

    fd = sys.stdin.fileno()
    cache: dict[str, list[Child]] = {}

    def dims() -> tuple[int, int, int]:
        cols, rows = shutil.get_terminal_size((80, 24))
        return cols, rows, max(1, rows - 5)

    def draw_loading(path: str, done: int = 0, total: int = 0, name: str = "") -> None:
        cols, _, _ = dims()
        out = ["\033[H",
               " " + disk_summary_line() + "\033[K\r\n",
               " " + bold("📁 " + _shorten(path, cols - 6)) + "\033[K\r\n",
               dim(" " + "─" * (cols - 2)) + "\033[K\r\n\r\n"]
        if total:
            out.append(f"   ⏳ scanning…  {done}/{total}  {dim(name[:40])}\033[K\r\n")
        else:
            out.append("   ⏳ scanning…\033[K\r\n")
        out.append("\033[J")
        _w("".join(out))
        _flush()

    def load(path: str) -> list[Child]:
        if path in cache:
            return cache[path]
        draw_loading(path)
        try:
            entries = list(os.scandir(path))
        except OSError:
            cache[path] = []
            return []
        kids: list[Child] = []
        n = len(entries)
        for i, e in enumerate(entries):
            if i % 4 == 0 or i == n - 1:
                draw_loading(path, i + 1, n, e.name)
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            kids.append(Child(e.name, e.path, tree_size(e.path), is_dir))
        kids.sort(key=lambda c: c.size, reverse=True)
        cache[path] = kids
        return kids

    def render(path: str, kids: list[Child], sel: int, scroll: int,
               message: str = "") -> None:
        cols, _, visible = dims()
        ck, ct = classify(path)
        note = ""
        if ck in ("exact", "inside") and ct:
            note = "  " + SAFETY_COLOR[ct.safety](SAFETY_PLAIN[ct.safety])
        out = ["\033[H",
               " " + disk_summary_line() + "\033[K\r\n",
               " " + bold("📁 " + _shorten(path, cols - 10)) + note + "\033[K\r\n",
               dim(" " + "─" * (cols - 2)) + "\033[K\r\n"]
        total = max(1, sum(c.size for c in kids))
        rendered = 0
        if not kids:
            out.append("   " + dim("(empty or unreadable)") + "\033[K\r\n")
            rendered = 1
        else:
            for idx in range(scroll, min(scroll + visible, len(kids))):
                out.append(_row(kids[idx], total, idx == sel, cols) + "\033[K\r\n")
                rendered += 1
        out.extend(["\033[K\r\n"] * (visible - rendered))
        out.append(dim(" " + "─" * (cols - 2)) + "\033[K\r\n")
        if message:
            out.append(" " + message + "\033[K")
        else:
            pos = f"{sel + 1}/{len(kids)}" if kids else "0/0"
            out.append(" " + dim("↑↓ move  → open  ← back  d delete  o Finder  q quit")
                       + dim(f"   [{pos}]") + "\033[K")
        out.append("\033[J")
        _w("".join(out))
        _flush()

    def fix_scroll(sel: int, scroll: int) -> int:
        _, _, visible = dims()
        if sel < scroll:
            return sel
        if sel >= scroll + visible:
            return sel - visible + 1
        return scroll

    old_attr = termios.tcgetattr(fd)
    current = start
    sel = scroll = 0
    message = ""
    try:
        tty.setraw(fd)
        _w("\033[?1049h\033[?25l")  # alt screen + hide cursor
        _flush()
        kids = load(current)
        while True:
            scroll = fix_scroll(sel, scroll)
            render(current, kids, sel, scroll, message)
            message = ""
            key = _read_key(fd)

            if key in ("q", "Q", "ESC", "CTRL_C"):
                break
            elif key in ("UP", "k"):
                sel = max(0, sel - 1)
            elif key in ("DOWN", "j"):
                sel = min(len(kids) - 1, sel + 1) if kids else 0
            elif key in ("g", "HOME"):
                sel = 0
            elif key in ("G", "END"):
                sel = max(0, len(kids) - 1)
            elif key in ("RIGHT", "l", "ENTER"):
                if kids and kids[sel].is_dir:
                    current = kids[sel].path
                    kids = load(current)
                    sel = scroll = 0
            elif key in ("LEFT", "h", "BACKSPACE"):
                parent = os.path.dirname(current)
                if parent and parent != current:
                    prev = os.path.realpath(current)
                    current = parent
                    kids = load(current)
                    sel = scroll = 0
                    for i, c in enumerate(kids):
                        if os.path.realpath(c.path) == prev:
                            sel = i
                            break
            elif key in ("o", "O"):
                if kids:
                    try:
                        subprocess.run(["open", "-R", kids[sel].path],
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
                        message = green("Revealed in Finder.")
                    except Exception:
                        message = red("Could not open Finder.")
            elif key in ("d", "D"):
                if kids:
                    c = kids[sel]
                    if _is_protected(c.path):
                        message = red("Refusing to delete a protected path.")
                    else:
                        safety, _t = _tag_text(c.path)
                        warn = "" if safety else "  ⚠ NOT a known cache!"
                        render(current, kids, sel, scroll,
                               red(f"Delete '{c.name}' ({human(c.size)})?{warn}  "
                                   "y = confirm, any other key = cancel"))
                        if _read_key(fd) in ("y", "Y"):
                            freed = delete_path(c.path)
                            cache.clear()
                            kids = load(current)
                            sel = min(sel, max(0, len(kids) - 1))
                            message = green(f"Freed {human(freed)}.")
                        else:
                            message = dim("Cancelled.")
            # any other key: just redraw
    finally:
        _w("\033[?25h\033[?1049l")  # show cursor + leave alt screen
        _flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
    return 0


PROTECTED = {
    "/", "/System", "/Library", "/Users", "/Applications", "/bin", "/usr",
    "/etc", "/var", "/private", HOME, os.path.dirname(HOME),
}


def _is_protected(path: str) -> bool:
    rp = os.path.realpath(path)
    if rp in {os.path.realpath(p) for p in PROTECTED}:
        return True
    # refuse anything shallower than 3 path components under root
    return rp.count(os.sep) < 3


def delete_path(path: str) -> int:
    """Permanently delete a file or directory. Returns bytes freed."""
    if _is_protected(path):
        return 0
    freed = tree_size(path)
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        else:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        return 0
    return freed


def empty_contents(path: str) -> int:
    """Delete everything inside a directory but keep the directory."""
    freed = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                freed += delete_path(entry.path)
    except OSError:
        pass
    return freed


@dataclass
class Hit:
    target: Target
    path: str
    size: int = 0


def collect_hits(categories: set[str] | None, max_safety_rank: int) -> list[Hit]:
    rank = {"safe": 0, "caution": 1, "risky": 2}
    hits: list[Hit] = []
    for t in REGISTRY:
        if categories and t.category not in categories:
            continue
        for p in expand_target(t):
            status(f"  sizing {os.path.basename(p)[:40]}…")
            size = tree_size(p)
            if size > 0:
                hits.append(Hit(t, p, size))
    clear_status()
    return hits


def cmd_suggest(args) -> int:
    categories = set(args.category) if args.category else None
    print()
    print_disk_summary()
    status("  scanning known cache locations…")
    hits = collect_hits(categories, 2)
    clear_status()
    if not hits:
        print(dim("  No known caches found."))
        return 0

    by_cat: dict[str, list[Hit]] = {}
    for h in hits:
        by_cat.setdefault(h.target.category, []).append(h)

    cat_titles = {
        "browser": "🌐 Browser caches",
        "xcode": "🔨 Xcode / Apple dev",
        "dev": "📦 Dev tool caches",
        "system": "🗂  System / misc",
    }
    grand_safe = 0
    grand_all = 0
    for cat in ["browser", "xcode", "dev", "system"]:
        if cat not in by_cat:
            continue
        print(bold(f"  {cat_titles.get(cat, cat)}"))
        # aggregate per target
        per_target: dict[str, list[Hit]] = {}
        for h in by_cat[cat]:
            per_target.setdefault(h.target.name, []).append(h)
        for name, group in sorted(per_target.items(),
                                  key=lambda kv: -sum(h.size for h in kv[1])):
            tsize = sum(h.size for h in group)
            t = group[0].target
            grand_all += tsize
            if t.safety == "safe":
                grand_safe += tsize
            print(f"    {bold(human(tsize)):>9}  {SAFETY_TAG[t.safety]}  {name}")
            print(f"               {dim(t.note)}")
        print()

    print(dim("  " + "─" * 60))
    print(f"  Reclaimable now (safe):     {green(bold(human(grand_safe)))}")
    print(f"  Reclaimable incl. caution:  {yellow(bold(human(grand_all)))}")
    print()
    print(dim("  Next: `mac_cleaner.py clean` (safe only) "
              "or `clean --include-caution`."))
    print(dim("  Add --apply to actually delete; otherwise it's a dry run."))
    print()
    return 0


def cmd_clean(args) -> int:
    categories = set(args.category) if args.category else None
    print()

    # decide which safety levels are in scope
    levels = {"safe"}
    if args.include_caution:
        levels.add("caution")

    status("  scanning…")
    all_hits = collect_hits(categories, 2)
    clear_status()
    hits = [h for h in all_hits if h.target.safety in levels]

    if not hits:
        print(dim("  Nothing to clean for the selected scope."))
        # still surface risky items so the user knows they exist
        risky = [h for h in all_hits if h.target.safety == "risky"]
        if risky:
            print()
            print(yellow("  Not touched (review manually):"))
            for h in risky:
                print(f"    {human(h.size):>9}  {h.target.name}  {dim(h.path)}")
        return 0

    total = sum(h.size for h in hits)
    print(bold("  Will clean:"))
    for h in sorted(hits, key=lambda x: -x.size):
        print(f"    {bold(human(h.size)):>9}  {SAFETY_TAG[h.target.safety]}  "
              f"{h.target.name}")
        print(f"               {dim(h.path)}")
    print(dim("  " + "─" * 60))
    print(f"  Total to reclaim: {green(bold(human(total)))}")
    print()

    if not args.apply:
        print(yellow("  Dry run — nothing deleted. Re-run with --apply to remove."))
        print()
        return 0

    if not args.yes:
        try:
            confirm = input(red(f"  Permanently delete the above ({human(total)})? "
                                "[y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if confirm not in ("y", "yes"):
            print(dim("  Cancelled."))
            return 0

    freed = 0
    for h in hits:
        status(f"  cleaning {h.target.name}…")
        if h.target.contents_only and os.path.isdir(h.path):
            freed += empty_contents(h.path)
        else:
            freed += delete_path(h.path)
    clear_status()
    print(green(bold(f"  ✓ Freed {human(freed)}.")))
    print()
    print_disk_summary()
    return 0


def parse_size(s: str) -> int:
    s = s.strip().upper()
    mult = 1
    for suffix, m in [("T", 1024**4), ("G", 1024**3), ("M", 1024**2),
                      ("K", 1024), ("B", 1)]:
        if s.endswith(suffix):
            mult = m
            s = s[:-1]
            break
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Show the actual invocation name (e.g. `mac-cleaner` when installed,
    # or `mac_cleaner.py` when run as a script) in usage/help text.
    prog = os.path.basename(sys.argv[0]) or "mac-cleaner"
    p = argparse.ArgumentParser(
        prog=prog,
        description="Explore disk usage and clean caches on macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""examples:
  {prog} suggest                  # what can I reclaim?
  {prog} scan ~ --depth 2         # biggest folders under home
  {prog} explore ~/Library        # drill in interactively
  {prog} clean                    # dry-run of safe cleanup
  {prog} clean --apply            # actually clean safe caches
  {prog} clean xcode dev --apply  # only Xcode + dev caches
  {prog} clean --include-caution --apply
""")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("scan", help="show folder sizes, level by level")
    sp.add_argument("path", nargs="?", default="~", help="folder to scan (default: ~)")
    sp.add_argument("--depth", type=int, default=1, help="levels to descend (default 1)")
    sp.add_argument("--top", type=int, default=20,
                    help="show top N per level (0 = all, default 20)")
    sp.add_argument("--min-size", default=None,
                    help="hide items smaller than this, e.g. 100M")
    sp.set_defaults(func=cmd_scan)

    ep = sub.add_parser(
        "explore",
        help="Finder-style browser: arrow keys to navigate, d to delete")
    ep.add_argument("path", nargs="?", default="~", help="start folder (default: ~)")
    ep.set_defaults(func=cmd_explore)

    gp = sub.add_parser("suggest", help="report reclaimable cache space")
    gp.add_argument("category", nargs="*",
                    help="limit to: browser xcode dev system")
    gp.set_defaults(func=cmd_suggest)

    cp = sub.add_parser("clean", help="delete known-safe caches (with confirm)")
    cp.add_argument("category", nargs="*",
                    help="limit to: browser xcode dev system")
    cp.add_argument("--apply", action="store_true",
                    help="actually delete (default is dry-run)")
    cp.add_argument("--include-caution", action="store_true",
                    help="also clean 'caution' items (Trash, logs, module caches…)")
    cp.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt (with --apply)")
    cp.set_defaults(func=cmd_clean)

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        print()
        print_disk_summary()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130


def cli() -> None:
    """Console-script entry point (used by `mac-cleaner`)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
