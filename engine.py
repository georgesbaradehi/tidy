"""
Tidy engine: scans the user's home folder, asks Claude for a structure, validates,
moves files with a journal, and can undo.

Claude only proposes. Every file operation is done here, by deterministic code.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import logging
import time
from collections import Counter
from pathlib import Path

HOME = Path(os.environ.get("TIDY_HOME", str(Path.home())))
DATA_DIR = Path(os.environ.get("TIDY_DATA", str(Path.home() / ".tidy")))
IS_MAC = sys.platform == "darwin"
logging.getLogger("pypdf").setLevel(logging.CRITICAL)

MODEL = os.environ.get("TIDY_MODEL", "claude-opus-5-5")
BATCH_SIZE = 80
DESKTOP_MAX = 12
MAX_SURVEY_DIRS = 3000
UNSORTED = "Documents/Archive/Unsorted"
DEST_ROOTS = ["Documents", "Pictures", "Movies", "Music"]
STANDARD_ROOTS = {"Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music"}

# Never touched: moving these breaks macOS or installed apps.
EXCLUDED_TOP = {"Library", "Applications", "Public", ".Trash"}

# Folder types whose inside must never change (move/rename the top folder only).
BUNDLE_EXTS = {
    ".app", ".key", ".pages", ".numbers", ".rtfd", ".xcodeproj", ".xcworkspace", ".playground",
    ".fcpbundle", ".fcpxbundle", ".logicx", ".band", ".sparsebundle", ".bundle", ".framework",
    ".plugin", ".aep", ".sketch", ".scriv", ".graffle", ".dtBase2", ".webarchive", ".pkg",
    ".mpkg", ".kext", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary",
    ".lrlibrary", ".lrdata", ".aplibrary", ".photolibrary", ".dvdmedia", ".lproj", ".vst3",
    ".component", ".mlpackage", ".mlmodelc", ".numbers-tef", ".band", ".garageband",
}
RELINK_EXTS = {".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".lrlibrary",
               ".aplibrary", ".photolibrary"}
WEBSITE_MARKERS = {"index.html", "index.htm", "index.php"}
PROJECT_MARKERS = {
    ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py", "requirements.txt",
    "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml", "build.gradle", "Makefile",
    "CMakeLists.txt", "Package.swift", "Podfile", "pubspec.yaml", "Dockerfile",
    "docker-compose.yml", ".vscode", ".idea", "Info.plist", "manifest.json", "config.toml",
    "_config.yml", "mkdocs.yml", "renv.lock", "environment.yml", "Pipfile", ".project",
}
PROJECT_FILE_EXTS = {  # a file of this type means its folder holds a project with linked media
    ".sln", ".csproj", ".prproj", ".als", ".ptx", ".rpp", ".flp", ".cpr", ".blend", ".unity",
    ".uproject", ".godot", ".aep", ".drp", ".fcpxml", ".psd-project", ".indd", ".idml",
    ".sublime-project", ".code-workspace", ".lrcat", ".cubase", ".sesx", ".veg", ".kdenlive",
    ".mlt", ".qgz", ".qgs", ".ipynb", ".Rproj", ".mcworld", ".sqlite-wal",
}
DATABASE_EXTS = {".sqlite", ".db", ".sqlite3", ".realm", ".mdb", ".accdb"}
PARTIAL_EXTS = {".crdownload", ".download", ".part", ".partial", ".opdownload", ".tmp"}
MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff", ".raw", ".cr2", ".cr3",
    ".nef", ".arw", ".dng", ".orf", ".raf", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mp3",
    ".m4a", ".wav", ".aiff", ".aif", ".flac", ".aac", ".ogg",
}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".py",
    ".js", ".ts", ".html", ".htm", ".css", ".sh", ".swift", ".java", ".c", ".h", ".cpp", ".rb",
    ".go", ".rs", ".php", ".sql", ".ini", ".cfg", ".toml", ".tex", ".srt", ".rtf",
}
LINK_EXTS = {".html", ".htm", ".css", ".js", ".mjs", ".md", ".markdown", ".xml", ".svg",
             ".json", ".tex", ".php", ".m3u", ".m3u8", ".cue", ".fcpxml", ".xspf", ".vue", ".jsx"}
TEXTUTIL_EXTS = {".doc", ".docx", ".odt", ".wordml"}
SF_DATALESS = 0x40000000  # iCloud "Optimize Storage" file that is not on disk

REF_RE = re.compile(
    r"""(?:src|href|url|import|from|require|include|path|file|source|data|poster)\s*[=:(]\s*\(?\s*["']([^"'<>\n]{1,300})["']"""
    r"""|url\(\s*([^)"'\s]{1,300})\s*\)"""
    r"""|\]\(\s*([^)\s]{1,300})\s*\)""",
    re.I,
)


# ---------------------------------------------------------------------------- helpers

def rel(p) -> str:
    try:
        return str(Path(p).relative_to(HOME))
    except ValueError:
        return str(p)


def split_name(name: str) -> tuple[str, str]:
    low = name.lower()
    for d in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if low.endswith(d):
            return name[: -len(d)], name[-len(d):]
    stem, ext = os.path.splitext(name)
    return (stem, ext) if stem else (name, "")


_BAD = re.compile(r'[/:\\\x00-\x1f]')


def clean_name(name: str, fallback: str) -> str:
    name = re.sub(r"\s+", " ", _BAD.sub("-", name or "")).strip().strip(".").strip()
    name = name or fallback
    while len(name.encode()) > 200:  # stay well under APFS's 255-byte limit
        name = name[:-1]
    return name


def clean_folder(path: str) -> str | None:
    if path == "Desktop":
        return "Desktop"
    parts = [clean_name(p, "") for p in (path or "").split("/") if p.strip()]
    parts = [p for p in parts if p and p not in (".", "..")]
    if not parts or parts[0] not in DEST_ROOTS:
        return None
    return "/".join(parts[:6])


def hidden(name: str) -> bool:
    return name.startswith(".") or name == "Icon\r"


def inside(p: str, containers) -> bool:
    return any(p == c or p.startswith(c + os.sep) for c in containers)


def available_roots() -> list[dict]:
    out = []
    for e in sorted(os.listdir(HOME)):
        p = HOME / e
        if hidden(e) or e in EXCLUDED_TOP or p.is_symlink() or not p.is_dir():
            continue
        out.append({"name": e, "default": True})
    return out


class Cancelled(Exception):
    pass


class Job:
    """Shared progress state read by the UI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "idle"  # idle | working | review | applying | done | error
        self.step = ""
        self.detail = ""
        self.progress = 0.0
        self.error = ""
        self.cancel = False
        self.result: dict = {}

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
        if self.cancel:
            raise Cancelled()

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "step": self.step, "detail": self.detail,
                    "progress": round(self.progress, 4), "error": self.error}


# ---------------------------------------------------------------------------- scanning

def snippet(p: Path, st: os.stat_result, n: int = 500) -> str:
    if st.st_size > 20 * 1024 * 1024 or (getattr(st, "st_flags", 0) & SF_DATALESS):
        return ""
    ext = p.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            with open(p, "r", errors="ignore") as f:
                txt = f.read(n * 3)
            if ext in {".html", ".htm", ".xml", ".rtf"}:
                txt = re.sub(r"<[^>]+>|\\[a-z]+\d* ?|[{}]", " ", txt)
        elif ext in TEXTUTIL_EXTS and IS_MAC:
            txt = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(p)],
                                 capture_output=True, text=True, timeout=10).stdout
        elif ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            txt = " ".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:2])
        else:
            return ""
    except Exception:
        return ""
    return re.sub(r"\s+", " ", txt).strip()[:n]


def where_from(p: Path) -> str:
    if not IS_MAC:
        return ""
    try:
        out = subprocess.run(["mdls", "-raw", "-name", "kMDItemWhereFroms", str(p)],
                             capture_output=True, text=True, timeout=5).stdout
        urls = re.findall(r'"([^"]+)"', out)
        return urls[0][:150] if urls else ""
    except Exception:
        return ""


def marker_kind(d: Path, entries: list[str]) -> str | None:
    ext = d.suffix.lower()
    if ext in BUNDLE_EXTS or ("Contents" in entries and (d / "Contents" / "Info.plist").exists()):
        return "App library" if ext in RELINK_EXTS else "Bundle"
    low = {e.lower() for e in entries}
    if low & WEBSITE_MARKERS:
        return "Website"
    if set(entries) & PROJECT_MARKERS:
        return "Project"
    exts = {os.path.splitext(e)[1].lower() for e in entries}
    if exts & PROJECT_FILE_EXTS:
        return "Project"
    if exts & DATABASE_EXTS:
        return "Data"
    return None


def folder_stats(p: Path) -> tuple[int, int, float]:
    n = size = 0
    newest = 0.0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            try:
                st = os.lstat(os.path.join(dp, f))
            except OSError:
                continue
            n += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return n, size, newest


def find_links(text_files: list[Path], roots: list[Path]):
    """Files that point at other files by relative path make their folder fragile.
    Returns (fragile_dirs, warnings)."""
    fragile, warnings = set(), []
    root_set = {str(r) for r in roots} | {str(HOME)}
    for f in text_files:
        try:
            with open(f, "r", errors="ignore") as fh:
                text = fh.read(512 * 1024)
        except OSError:
            continue
        for m in REF_RE.finditer(text):
            ref = next(g for g in m.groups() if g)
            ref = ref.split("?")[0].split("#")[0].strip()
            if not ref or re.match(r"^([a-z][a-z0-9+.-]*:|//|#|\{|\$)", ref, re.I) and not ref.startswith("file:"):
                continue
            if ref.startswith("file://"):
                ref = ref[7:]
            if ref.startswith("/"):
                if ref.startswith(str(HOME)):
                    warnings.append(f"{rel(f)} uses an absolute link to {rel(ref)}; it breaks if that file moves")
                continue
            try:
                target = (f.parent / ref).resolve()
            except (OSError, ValueError):
                continue
            if target == f or not target.exists() or not str(target).startswith(str(HOME)):
                continue
            common = Path(os.path.commonpath([str(f.parent), str(target.parent if target.is_file() else target)]))
            if str(common) in root_set or not str(common).startswith(str(HOME) + os.sep):
                warnings.append(f"{rel(f)} links to {rel(target)} across top-level folders; keep an eye on it")
                continue
            fragile.add(str(common))
    return fragile, warnings


def scan(job: Job, roots: list[Path], read_content: bool):
    files, units, dirs, skipped, text_files = [], [], [], [], []
    count = [0]

    def walk(d: Path, root: Path, depth: int):
        try:
            entries = sorted(os.listdir(d))
        except OSError as e:
            skipped.append([rel(d), f"Can't read ({e.__class__.__name__})"])
            return
        ext_counts, samples, n = Counter(), [], 0
        for name in entries:
            if hidden(name):
                continue
            p = d / name
            if p.is_symlink():
                skipped.append([rel(p), "Shortcut (symlink)"])
                continue
            if p.is_dir():
                try:
                    sub = os.listdir(p)
                except OSError:
                    skipped.append([rel(p), "Can't read"])
                    continue
                kind = marker_kind(p, sub)
                if kind:
                    units.append({"path": str(p), "kind": kind, "entries": sub})
                else:
                    walk(p, root, depth + 1)
            elif p.is_file():
                ext = p.suffix.lower()
                if ext in PARTIAL_EXTS:
                    skipped.append([rel(p), "Unfinished download"])
                    continue
                st = p.stat()
                files.append({
                    "path": str(p), "size": st.st_size, "mtime": st.st_mtime,
                    "btime": getattr(st, "st_birthtime", st.st_mtime),
                    "snippet": snippet(p, st) if read_content else "",
                    "from": where_from(p) if root.name == "Downloads" else "",
                })
                if ext in LINK_EXTS and st.st_size < 2 * 1024 * 1024:
                    text_files.append(p)
                n += 1
                ext_counts[ext or "(none)"] += 1
                if len(samples) < 8:
                    samples.append(name)
                count[0] += 1
                if count[0] % 500 == 0:
                    job.set(detail=f"{count[0]:,} files found")
        if d != root:
            dirs.append({"path": str(d), "depth": depth, "n": n, "exts": dict(ext_counts.most_common(5)),
                         "samples": samples})

    for r in roots:
        job.set(detail=f"Looking in {r.name}…")
        kind = None if r.name in STANDARD_ROOTS else marker_kind(r, os.listdir(r))
        if kind:  # a custom top-level folder that is itself a project
            units.append({"path": str(r), "kind": kind, "entries": os.listdir(r)})
        else:
            walk(r, r, 0)

    # Folders whose files link to each other are fragile too.
    job.set(detail="Checking which folders depend on their structure…")
    linked, link_warnings = find_links(text_files, roots)
    for d in linked:
        units.append({"path": d, "kind": "Linked files", "entries": os.listdir(d)})

    # Keep only the outermost fragile folder.
    units.sort(key=lambda u: len(u["path"]))
    kept, seen = [], set()
    for u in units:
        if u["path"] in seen or inside(u["path"], [k["path"] for k in kept]):
            continue
        seen.add(u["path"])
        kept.append(u)
    unit_paths = [u["path"] for u in kept]
    files = [f for f in files if not inside(f["path"], unit_paths)]
    dirs = [d for d in dirs if not inside(d["path"], unit_paths)]
    for u in kept:
        n, size, newest = folder_stats(Path(u["path"]))
        u.update(n=n, size=size, mtime=newest or os.stat(u["path"]).st_mtime,
                 contents=[e for e in sorted(u.pop("entries")) if not hidden(e)][:12])
    return files, kept, dirs, skipped, link_warnings


# ---------------------------------------------------------------------------- Claude

def ask(client, system: str, user: str, schema: dict, effort: str, max_tokens: int) -> dict:
    import anthropic
    kw = dict(model=MODEL, max_tokens=max_tokens,
              system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
              messages=[{"role": "user", "content": user}],
              output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    last = None
    for attempt in range(4):
        try:
            # If a safety filter wrongly declines (odd file names), another model finishes.
            with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"],
                                             fallbacks="default", **kw) as s:
                msg = s.get_final_message()
            if msg.stop_reason in ("refusal", "max_tokens"):
                raise ValueError(f"stopped: {msg.stop_reason}")
            return json.loads(next(b.text for b in msg.content if b.type == "text"))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            last = e
            time.sleep(8 * (attempt + 1))
        except (ValueError, StopIteration) as e:
            last = e
    raise RuntimeError(f"Claude request failed: {last}")


SURVEY_SYSTEM = f"""You are an expert digital archivist designing the folder structure for one person's entire Mac.

You receive a survey of their files: folders (with counts, file types, sample names) and
"locked" items: folders whose inside must never change (websites, code projects, app
libraries, media projects, folders whose files link to each other). Locked items can be
moved and renamed as a whole.

Return:
1. taxonomy: a clear, human folder hierarchy tailored to THIS person's real files.
   - Every path starts with one of: {", ".join(DEST_ROOTS)}.
     Documents = documents, projects, software, archives. Pictures = photos/images.
     Movies = video. Music = audio.
   - 2-4 levels deep, Title Case, no numbering. Specific but never one-file folders.
   - Must include "{UNSORTED}". Typically 30-150 folders.
2. locked: for EVERY locked id, a dest_folder (taxonomy path) and a clear new_name for the
   top folder. Keep bundle extensions (e.g. "Q3 Review.key", "Family.photoslibrary").
3. collections: ids of ordinary folders that should ALSO stay intact because their files
   belong together and their internal structure matters (a photo shoot, a trip, an album,
   a course, a dataset, a client delivery, a game or tool folder, anything whose layout an
   app or person depends on). Never pick generic dumping grounds. Never pick a folder and
   one of its own subfolders. Give dest_folder and new_name.
4. desktop: at most {DESKTOP_MAX} locked/collection ids for current, active work. Often fewer.
5. naming_rules: a short, consistent style guide for renaming individual files.
"""

_obj = lambda props: {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
_placed = _obj({"id": {"type": "string"}, "dest_folder": {"type": "string"}, "new_name": {"type": "string"}})
SURVEY_SCHEMA = _obj({
    "taxonomy": {"type": "array", "items": _obj({"path": {"type": "string"}, "description": {"type": "string"}})},
    "locked": {"type": "array", "items": _placed},
    "collections": {"type": "array", "items": _placed},
    "desktop": {"type": "array", "items": {"type": "string"}},
    "naming_rules": {"type": "string"},
})


def survey_prompt(units, dirs, n_files) -> str:
    lines = [f"Today is {dt.date.today()}. Loose files: {n_files:,}.", "",
             "## Folders (id | path | files | types | samples)"]
    for d in dirs:
        ex = " ".join(f"{k}:{v}" for k, v in d["exts"].items())
        lines.append(f'{d["id"]} | {rel(d["path"])} | {d["n"]} | {ex} | {"; ".join(d["samples"])}')
    lines += ["", "## Locked (id | kind | path | files | last modified | contents)"]
    for u in units:
        lines.append(f'{u["id"]} | {u["kind"]} | {rel(u["path"])} | {u["n"]} | '
                     f'{dt.date.fromtimestamp(u["mtime"])} | {", ".join(u["contents"])}')
    return "\n".join(lines)


def classify_system(taxonomy, rules, rename_media: bool) -> str:
    tax = "\n".join(f'- {t["path"]}: {t["description"]}' for t in taxonomy)
    media = "" if rename_media else "\nFor photos, video and audio files, keep the original name (just tidy it)."
    return f"""You file and rename files on a Mac according to an agreed folder structure.

Folder structure:
{tax}

Naming rules:
{rules}{media}

For EVERY file return:
- dest_folder: exactly one folder path above ({UNSORTED} if unsure).
- new_name: a clear, descriptive name WITHOUT the extension (it is kept automatically).
  Use the content, source URL, dates and original name. Keep identifiers (invoice numbers,
  versions). Never invent facts. If the original name is already good, keep it.
- desktop: true only for clearly current, active work. Almost always false.
"""


def classify_schema(paths) -> dict:
    item = _obj({"id": {"type": "string"}, "dest_folder": {"type": "string", "enum": paths},
                 "new_name": {"type": "string"}, "desktop": {"type": "boolean"}})
    return _obj({"files": {"type": "array", "items": item}})


def file_line(i: str, f: dict) -> str:
    parts = [i, rel(f["path"]), f'{f["size"] // 1024}KB',
             f'created {dt.date.fromtimestamp(f["btime"])}', f'modified {dt.date.fromtimestamp(f["mtime"])}']
    if f["from"]:
        parts.append("downloaded from " + f["from"])
    if f["snippet"]:
        parts.append("content: " + f["snippet"])
    return " | ".join(parts)


# ---------------------------------------------------------------------------- planning

def build_plan(job: Job, client, roots: list[str], read_content=True, rename_media=True, workers=4) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root_paths = [HOME / r for r in roots if (HOME / r).is_dir() and r not in EXCLUDED_TOP]

    job.set(step="Scanning your files", progress=0.02, detail="")
    files, units, dirs, skipped, warnings = scan(job, root_paths, read_content)
    if not files and not units:
        raise RuntimeError("No files found in the selected folders.")

    for i, u in enumerate(units):
        u["id"] = f"L{i}"
    survey_dirs = sorted([d for d in dirs if d["n"]], key=lambda d: (d["depth"], -d["n"]))[:MAX_SURVEY_DIRS]
    for i, d in enumerate(survey_dirs):
        d["id"] = f"D{i}"

    job.set(step="Designing your folders", progress=0.12,
            detail=f"{len(files):,} files and {len(units):,} locked folders")
    s = ask(client, SURVEY_SYSTEM, survey_prompt(units, survey_dirs, len(files)), SURVEY_SCHEMA, "high", 64000)

    taxonomy, seen = [], set()
    for t in s["taxonomy"]:
        p = clean_folder(t["path"])
        if p and p != "Desktop" and p not in seen:
            seen.add(p)
            taxonomy.append({"path": p, "description": t["description"]})
    if UNSORTED not in seen:
        taxonomy.append({"path": UNSORTED, "description": "Anything unclear"})
    paths = [t["path"] for t in taxonomy]
    desktop_ids = set(s["desktop"][:DESKTOP_MAX])

    by_id = {d["id"]: d for d in survey_dirs}
    colls, cpaths = [], []
    for c in sorted([c for c in s["collections"] if c["id"] in by_id], key=lambda c: len(by_id[c["id"]]["path"])):
        p = by_id[c["id"]]["path"]
        if not inside(p, cpaths):
            cpaths.append(p)
            colls.append(c)

    ops = []

    def add(kind, src, folder, name, desktop, locked, n=1, note=""):
        ops.append({"id": f"op{len(ops)}", "kind": kind, "src": src, "folder": clean_folder(folder) or UNSORTED,
                    "name": name, "desktop": desktop, "locked": locked, "n": n, "note": note,
                    "include": True, "mtime": os.stat(src).st_mtime})

    for c in colls:
        d = by_id[c["id"]]
        n, _, _ = folder_stats(Path(d["path"]))
        add("Collection", d["path"], c["dest_folder"], c["new_name"], c["id"] in desktop_ids, True, n)
    answers = {a["id"]: a for a in s["locked"]}
    for u in units:
        if inside(u["path"], cpaths):
            continue
        a = answers.get(u["id"], {})
        note = "The app that uses it may ask you to locate it once." if u["kind"] == "App library" else ""
        add(u["kind"], u["path"], a.get("dest_folder", UNSORTED), a.get("new_name") or Path(u["path"]).name,
            u["id"] in desktop_ids, True, u["n"], note)

    loose = [f for f in files if not inside(f["path"], cpaths)]
    cache_file = DATA_DIR / "cache.json"
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    key = json.dumps([paths, s["naming_rules"], rename_media])
    if cache.get("key") != key:
        cache = {"key": key, "files": {}}
    todo = [f for f in loose if f["path"] not in cache["files"]]
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    system, schema = classify_system(taxonomy, s["naming_rules"], rename_media), classify_schema(paths)

    def run(batch):
        if job.cancel:
            raise Cancelled()
        ids = {f"F{i}": f for i, f in enumerate(batch)}
        out = ask(client, system, "\n".join(file_line(i, f) for i, f in ids.items()), schema, "medium", 32000)
        return {ids[r["id"]]["path"]: r for r in out["files"] if r["id"] in ids}

    job.set(step="Sorting and renaming files", progress=0.2, detail=f"0 of {len(loose):,} files")
    done = len(loose) - len(todo)
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(run, b): len(b) for b in batches}
        for fut in cf.as_completed(futs):
            try:
                cache["files"].update(fut.result())
            except (Cancelled, Exception) as e:  # noqa: B014
                if job.cancel:
                    pool.shutdown(cancel_futures=True)
                    raise Cancelled()
                warnings.append(f"{futs[fut]} files were left in place because a request failed ({e})")
            done += futs[fut]
            cache_file.write_text(json.dumps(cache))
            job.set(progress=0.2 + 0.75 * done / max(len(loose), 1), detail=f"{done:,} of {len(loose):,} files")

    for f in loose:
        r = cache["files"].get(f["path"])
        if r:
            add("File", f["path"], r["dest_folder"], r["new_name"], r["desktop"], False)

    plan = {"created": dt.datetime.now().isoformat(timespec="seconds"), "roots": [str(r) for r in root_paths],
            "taxonomy": paths, "ops": ops, "warnings": warnings, "skipped": skipped, "rename_media": rename_media}
    (DATA_DIR / "plan.json").write_text(json.dumps(plan))
    return plan


def resolve(plan: dict, edits: dict | None = None) -> list[dict]:
    """Apply user edits, then validate into concrete, collision-free moves."""
    edits = edits or {}
    claimed: set[str] = set()
    ops = [dict(o, **edits.get(o["id"], {})) for o in plan["ops"]]
    ops = [o for o in ops if o.get("include", True)]

    desk = sorted([o for o in ops if o["desktop"]], key=lambda o: -o["mtime"])
    for o in desk[DESKTOP_MAX:]:
        o["desktop"] = False

    def unique(dst: Path, src: Path) -> Path:
        stem, ext = split_name(dst.name)
        cand, n = dst, 2
        while cand.as_posix().lower() in claimed or (cand.exists() and cand != src and not
                                                     (cand.as_posix().lower() == src.as_posix().lower())):
            cand = dst.with_name(f"{stem} ({n}){ext}")
            n += 1
        claimed.add(cand.as_posix().lower())
        return cand

    # Nothing may ever be placed inside a locked folder.
    locked_srcs = [o["src"] for o in plan["ops"] if o["locked"]]

    moves = []
    for o in sorted(ops, key=lambda o: (not o["locked"], o["id"])):
        src = Path(o["src"])
        old_stem, ext = split_name(src.name)
        keep_stem = (o["kind"] == "File" and not plan.get("rename_media", True) and ext.lower() in MEDIA_EXTS)
        wanted = old_stem if keep_stem else o["name"]
        if o["kind"] in ("File", "Bundle", "App library") and ext:
            stem, given = split_name(clean_name(wanted, old_stem))
            name = (stem if given.lower() == ext.lower() else clean_name(wanted, old_stem)) + ext
        else:
            name = clean_name(wanted, src.name)
        folder = clean_folder(o["folder"]) or UNSORTED
        base = HOME / "Desktop" if (o["desktop"] or folder == "Desktop") else HOME / folder
        if inside(str(base), [p for p in locked_srcs if p != o["src"]]):
            base = HOME / UNSORTED
        dst = unique(base / name, src)
        if dst == src:
            continue
        if o["locked"] and (dst.as_posix() + "/").startswith(src.as_posix() + "/"):
            continue
        moves.append({"id": o["id"], "kind": o["kind"], "locked": o["locked"], "n": o["n"],
                      "src": str(src), "dst": str(dst), "note": o.get("note", "")})
    return moves


def preview(plan: dict) -> dict:
    """What the UI shows: proposed moves plus folder list."""
    moves = {m["id"]: m for m in resolve(plan)}
    items = []
    for o in plan["ops"]:
        m = moves.get(o["id"])
        dst = Path(m["dst"]) if m else Path(o["src"])
        items.append({"id": o["id"], "kind": o["kind"], "locked": o["locked"], "n": o["n"], "note": o["note"],
                      "from": rel(o["src"]), "name": dst.name, "folder": rel(dst.parent),
                      "unchanged": m is None})
    return {"items": items, "folders": ["Desktop"] + plan["taxonomy"], "warnings": plan["warnings"],
            "skipped": plan["skipped"], "created": plan["created"]}


# ---------------------------------------------------------------------------- apply / undo

def _move(src: Path, dst: Path, journal=None):
    if journal is not None:  # record folders we create, so undo can remove them
        missing, d = [], dst.parent
        while not d.exists():
            missing.append(d)
            d = d.parent
        for d in reversed(missing):
            d.mkdir()
            journal.write(json.dumps({"mkdir": str(d)}) + "\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.parent == dst.parent and src.name.lower() == dst.name.lower():
        tmp = src.with_name(src.name + ".tidy-tmp")  # case-only rename on case-insensitive APFS
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        shutil.move(str(src), str(dst))


def snapshot_time_machine() -> bool:
    if not IS_MAC:
        return False
    try:
        return subprocess.run(["tmutil", "localsnapshot"], capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def apply(job: Job, plan: dict, edits: dict) -> dict:
    moves = resolve(plan, edits)
    job.set(step="Saving a Time Machine snapshot", progress=0.0, detail="")
    snap = snapshot_time_machine()
    journal = DATA_DIR / f"journal-{dt.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    ok, failed, problems, relink = 0, 0, [], []
    with open(journal, "a") as j:
        for i, m in enumerate(moves):
            src, dst = Path(m["src"]), Path(m["dst"])
            if i % 25 == 0:
                job.set(step="Organizing", progress=i / max(len(moves), 1), detail=f"{i:,} of {len(moves):,}")
            try:
                if not src.exists():
                    raise FileNotFoundError("it no longer exists")
                if dst.exists():
                    raise FileExistsError("something else now has that name")
                _move(src, dst, j)
                j.write(json.dumps({"src": str(src), "dst": str(dst)}) + "\n")
                j.flush()
                ok += 1
                if m["locked"]:  # verify the inside is complete
                    n, _, _ = folder_stats(dst)
                    if n != m["n"]:
                        problems.append(f"{dst.name}: expected {m['n']} files inside, found {n}")
                    if m["kind"] == "Website" and not any((dst / x).exists() for x in WEBSITE_MARKERS):
                        problems.append(f"{dst.name}: index file missing after move")
                if m["note"]:
                    relink.append(f"{dst.name}: {m['note']}")
            except Exception as e:
                failed += 1
                problems.append(f"{rel(src)} was not moved: {e}")
    removed = _remove_empty([Path(r) for r in plan["roots"]], journal)
    res = {"moved": ok, "failed": failed, "removed": removed, "problems": problems, "notes": relink,
           "snapshot": snap, "journal": str(journal)}
    (DATA_DIR / "last.json").write_text(json.dumps(res))
    return res


def _remove_empty(roots: list[Path], journal: Path) -> int:
    n = 0
    with open(journal, "a") as j:
        for root in roots:
            for dp, _, _ in os.walk(root, topdown=False):
                d = Path(dp)
                if d == root:
                    continue
                try:
                    if all(e in (".DS_Store", ".localized") for e in os.listdir(d)):
                        for e in os.listdir(d):
                            os.remove(d / e)
                        os.rmdir(d)
                        j.write(json.dumps({"rmdir": str(d)}) + "\n")
                        n += 1
                except OSError:
                    pass
    return n


def undo(job: Job, journal: str) -> dict:
    entries = [json.loads(l) for l in Path(journal).read_text().splitlines() if l.strip()]
    ok, problems = 0, []
    for i, e in enumerate(reversed(entries)):
        if i % 25 == 0:
            job.set(step="Putting everything back", progress=i / max(len(entries), 1), detail="")
        try:
            if "rmdir" in e:
                Path(e["rmdir"]).mkdir(parents=True, exist_ok=True)
                continue
            if "mkdir" in e:
                d = Path(e["mkdir"])
                if d.exists() and all(x in (".DS_Store", ".localized") for x in os.listdir(d)):
                    shutil.rmtree(d)
                continue
            src, dst = Path(e["src"]), Path(e["dst"])
            if src.exists():
                raise FileExistsError("something is already there")
            _move(dst, src)
            ok += 1
        except Exception as ex:
            problems.append(f"{rel(e.get('src', e.get('rmdir', '?')))}: {ex}")
    Path(journal).rename(Path(journal).with_suffix(".undone"))
    (DATA_DIR / "last.json").unlink(missing_ok=True)
    return {"restored": ok, "problems": problems}
