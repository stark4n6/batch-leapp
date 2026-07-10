#!/usr/bin/env python3
"""
sample_data_update.py — populate the `sample_data` key of LEAPP artifact
metadata from batch-leapp coverage databases.

Reads one or more batch_apps.sqlite files (produced by batch_leapp --coverage
/ batch_coverage.py) plus a samples.json corpus manifest, and writes/updates
each artifact's `__artifacts_v2__[...]["sample_data"]` dict in local iLEAPP /
ALEAPP checkouts. Entries follow the documented dict[str, str] format:

    "sample_data": {
        "josh_ios_15": "iOS 15.4.1 | TikTok 30.1.0 | 32 rows",
        "pixel6_a13": "Android 13 | com.viber.voip vc 22450123 | 0 rows",
    },

Rules:
- An artifact gets an entry for a sample when its search patterns matched at
  least one file in that extraction (0-row outcomes included).
- Only sample keys registered in samples.json are managed; hand-written keys
  are never touched. A managed key is removed only when its sample was
  present in this run's databases and no longer produces an entry.
- Edits preserve each file's newline style (CRLF kept), quote style, and key
  order; re-running with the same inputs is a no-op (idempotent).

Default is a dry run that prints the proposed changes; --apply edits the
repos and then validates (ast re-parse, py_compile, PluginLoader smoke,
pylint new-warning check), restoring the original files on any failure.

Usage:

    python3 sample_data_update.py \
        --db /corpus_out/ios/batch_apps.sqlite \
        --db /corpus_out/android/batch_apps.sqlite \
        --samples /corpus/samples.json \
        --ileapp ~/GitHub/iLEAPP --aleapp ~/GitHub/ALEAPP [--apply]

    # register any unmatched extractions as stub samples
    python3 sample_data_update.py --db ... --samples ... --init-samples
"""

import argparse
import ast
import json
import os
import py_compile
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

# output_types values that produce a LAVA table (framework: check_output_types;
# an absent output_types defaults to all types)
_LAVA_CAPABLE = {"lava", "standard", "all", "lava_only"}


# --------------------------------------------------------------------------
# samples.json
# --------------------------------------------------------------------------

def load_samples(path: Path):
    """samples.json -> OrderedDict of sample_key -> spec dict."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=OrderedDict)
    except FileNotFoundError:
        return OrderedDict()
    except json.JSONDecodeError as ex:
        raise SystemExit(f"error: {path} is not valid JSON: {ex}") from ex
    samples = data.get("samples", OrderedDict())
    for key, spec in samples.items():
        if not isinstance(spec, dict) or not spec.get("match", {}).get("zip"):
            raise SystemExit(
                f"error: samples.json entry '{key}' needs a match.zip value")
    return samples


def _sanitize_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def _zip_matches(want: str, ext) -> bool:
    """Match a samples.json match.zip value against an extraction.

    A value containing a path separator is compared against the archive's
    corpus-relative path (needed when several archives share a file name,
    e.g. Cellebrite's EXTRACTION_FFS.zip); a bare name matches the basename.
    """
    want = str(want).replace("\\", "/")
    ext_zip = (ext["zip"] or "").replace("\\", "/")
    if "/" in want:
        return ext_zip == want
    return Path(ext_zip).name == want or ext["input_name"] == want


def _derive_key(zip_rel: str, taken) -> str:
    """Stub key for an archive: its stem, disambiguated by parent path
    components when several archives share a name."""
    parts = Path(zip_rel.replace("\\", "/")).parts
    stem = _sanitize_key(Path(parts[-1]).stem) or "sample"
    candidates = [stem]
    if len(parts) > 1:
        candidates.append(_sanitize_key(f"{parts[0]}_{stem}"))
        candidates.append(_sanitize_key(
            "_".join(list(parts[:-1]) + [Path(parts[-1]).stem])))
    for candidate in candidates:
        if candidate and candidate not in taken:
            return candidate
    base, n = stem, 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def init_samples(samples_path: Path, samples, extractions, log=print):
    """Append stub entries for extractions not matched by any sample."""
    basenames = {}
    for ext in extractions:
        name = Path((ext["zip"] or ext["input_name"] or "")).name
        basenames[name] = basenames.get(name, 0) + 1
    added = 0
    for ext in extractions:
        zip_rel = (ext["zip"] or ext["input_name"] or "").replace("\\", "/")
        if not zip_rel:
            continue
        if any(_zip_matches(spec["match"]["zip"], ext)
               for spec in samples.values()):
            continue
        basename = Path(zip_rel).name
        # ambiguous basename -> register the corpus-relative path instead
        match_zip = zip_rel if (basenames.get(basename, 0) > 1
                                and "/" in zip_rel) else basename
        key = _derive_key(zip_rel, samples)
        samples[key] = OrderedDict((
            ("match", {"zip": match_zip}),
            ("platform", "ios" if ext["tool_norm"] == "ileapp" else
                         "android" if ext["tool_norm"] == "aleapp" else ""),
            ("os_version", None),
            ("app_versions", {}),
            ("notes", ""),
        ))
        added += 1
        log(f"samples: added stub '{key}' for {match_zip}")
    if added:
        with open(samples_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "samples": samples}, f, indent=2)
            f.write("\n")
        log(f"samples: wrote {samples_path} ({added} new stub(s)) — review "
            "keys/overrides, then re-run without --init-samples")
    else:
        log("samples: every extraction is already registered")
    return added


# --------------------------------------------------------------------------
# coverage-database assembly
# --------------------------------------------------------------------------

def _norm_tool(tool: str) -> str:
    tool = (tool or "").lower()
    for known in ("ileapp", "aleapp", "rleapp", "vleapp"):
        if known in tool:
            return known
    return tool


def load_extractions(db_paths):
    """All extraction rows across the coverage DBs."""
    rows = []
    for db_path in db_paths:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for (ext_id, zip_name, input_name, tool, os_version,
                 report_path, sha256) in con.execute(
                    "SELECT extraction_id, zip, input_name, tool,"
                    " os_version, report_path, sha256 FROM extractions"):
                rows.append({
                    "db": Path(db_path), "extraction_id": ext_id,
                    "zip": zip_name or "", "input_name": input_name or "",
                    "tool": tool or "", "tool_norm": _norm_tool(tool),
                    "os_version": os_version or "",
                    "report_path": report_path or "",
                    "sha256": sha256 or "",
                })
        finally:
            con.close()
    return rows


def match_samples(samples, extractions, pick_latest, warnings):
    """sample_key -> {tool_norm: extraction row}; duplicate (sample, tool)
    matches are a hard error unless --pick-latest."""
    resolved = {}
    for key, spec in samples.items():
        want_zip = spec["match"]["zip"]
        want_sha = (spec["match"].get("sha256") or "").lower()
        hits = [e for e in extractions if _zip_matches(want_zip, e)
                and (not want_sha or (e.get("sha256") or "").lower() == want_sha)]
        by_tool = {}
        for ext in hits:
            by_tool.setdefault(ext["tool_norm"], []).append(ext)
        chosen = {}
        for tool, tool_hits in by_tool.items():
            if len(tool_hits) > 1:
                if not pick_latest:
                    folders = ", ".join(h["report_path"] for h in tool_hits)
                    raise SystemExit(
                        f"error: sample '{key}' matches {len(tool_hits)} "
                        f"{tool} extractions ({folders}). Remove stale output "
                        "folders or pass --pick-latest.")
                def _mtime(h):
                    try:
                        return Path(h["report_path"]).stat().st_mtime
                    except OSError:
                        return h["extraction_id"]
                tool_hits.sort(key=_mtime)
            chosen[tool] = tool_hits[-1]
        platform = (spec.get("platform") or "").lower()
        expect = {"ios": "ileapp", "android": "aleapp"}.get(platform)
        if expect and chosen and expect not in chosen:
            warnings.append(
                f"sample '{key}': platform is '{platform}' but no {expect} "
                f"run matched (found: {', '.join(chosen) or 'none'})")
        resolved[key] = chosen
    return resolved


def load_sample_facts(sample_key, spec, ext, warnings):
    """Everything needed to build entries for one (sample, extraction)."""
    con = sqlite3.connect(f"file:{ext['db']}?mode=ro", uri=True)
    try:
        eid = ext["extraction_id"]
        # OS label: override (verbatim) > detected (prefixed) > unknown
        override = spec.get("os_version")
        if override:
            os_label = str(override)
        elif ext["os_version"]:
            prefix = {"ileapp": "iOS ", "aleapp": "Android "}.get(
                ext["tool_norm"], "")
            detected = ext["os_version"]
            os_label = detected if detected.lower().startswith(
                ("ios", "android")) else prefix + detected
        else:
            os_label = "OS unknown"
            warnings.append(
                f"sample '{sample_key}' ({ext['tool_norm']}): no OS version "
                "detected — set samples.json os_version")

        # app versions: DB facts, overlaid by samples.json overrides
        versions = {}
        kind_rank = {"ios_app_store": 2, "android_gass": 1,
                     "android_packages_xml": 0}
        for app_id, app_name, version, kind in con.execute(
                "SELECT app_id, app_name, version, version_kind"
                " FROM app_versions WHERE extraction_id = ?", (eid,)):
            rank = kind_rank.get(kind, -1)
            if app_id not in versions or rank > versions[app_id][2]:
                versions[app_id] = (app_name or "", str(version), rank, False)
        for app_id, version in (spec.get("app_versions") or {}).items():
            prev = versions.get(app_id)
            versions[app_id] = (prev[0] if prev else "", str(version), 99, True)

        # per-artifact results, owning apps and run errors
        results = {}
        for module, artifact, files_matched, row_count in con.execute(
                "SELECT module_name, artifact_key, files_matched, row_count"
                " FROM artifact_results WHERE extraction_id = ?", (eid,)):
            results[artifact] = (module, files_matched, row_count)
        apps = {}
        for artifact, app_id in con.execute(
                "SELECT DISTINCT artifact_key, owner_app_id FROM artifact_files"
                " WHERE extraction_id = ? AND owner_app_id != ''"
                " AND artifact_key != ''", (eid,)):
            apps.setdefault(artifact, set()).add(app_id)
        errors = {key for (key,) in con.execute(
            "SELECT artifact_key FROM artifact_errors"
            " WHERE extraction_id = ?", (eid,))}
    finally:
        con.close()
    return {"os_label": os_label, "versions": versions, "results": results,
            "apps": apps, "errors": errors}


# --------------------------------------------------------------------------
# entry generation
# --------------------------------------------------------------------------

def _app_part(app_ids, versions, tool_norm):
    """1-3 owning apps rendered with versions; None when 0 or >=4 apps."""
    apps = sorted(app_ids)
    if not apps or len(apps) >= 4:
        return None
    parts = []
    for app_id in apps:
        info = versions.get(app_id)
        if not info:
            parts.append(app_id)
            continue
        app_name, version, _rank, is_override = info
        if tool_norm == "aleapp" and not is_override:
            parts.append(f"{app_id} vc {version}")
        else:
            parts.append(f"{app_name or app_id} {version}")
    return ", ".join(parts)


def _rows_part(artifact_key, row_count, lava_capable, errors, warnings,
               sample_key):
    if artifact_key in errors:
        warnings.append(
            f"'{artifact_key}' errored during the {sample_key} run — "
            "no entry written")
        return None
    if row_count is not None:
        return f"{row_count} row" + ("" if row_count == 1 else "s")
    if lava_capable:
        return "0 rows"
    return "files found"


def build_entry(sample_key, facts, artifact_key, lava_capable, tool_norm,
                warnings):
    """The generated note string, or None when no entry should be written."""
    result = facts["results"].get(artifact_key)
    if not result or not result[1]:      # no files matched
        return None
    _module, _files_matched, row_count = result
    rows = _rows_part(artifact_key, row_count, lava_capable, facts["errors"],
                      warnings, sample_key)
    if rows is None:
        return None
    parts = [facts["os_label"]]
    app_part = _app_part(facts["apps"].get(artifact_key, ()),
                         facts["versions"], tool_norm)
    if app_part:
        parts.append(app_part)
    parts.append(rows)
    return " | ".join(parts)


# A generated value (used to flag replacement of hand-written prose)
_GENERATED_RE = re.compile(r"^(iOS |Android |OS unknown)")


# --------------------------------------------------------------------------
# repo scan (ast)
# --------------------------------------------------------------------------

class ArtifactFile:
    """One scripts/artifacts/*.py file with parsed metadata spans."""

    def __init__(self, path, source, newline, line_starts, artifacts):
        self.path = path
        self.source = source            # LF-normalized text
        self.newline = newline          # original: '\n' or '\r\n'
        self.line_starts = line_starts
        self.artifacts = artifacts      # key -> info dict
        self.edits = []                 # (start, end, replacement)


def _line_starts(text):
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _offset(line_starts, lineno, col):
    return line_starts[lineno - 1] + col


def _lava_capable(output_types_value):
    """True when the artifact writes a LAVA table (absent key = all types)."""
    if output_types_value is None:
        return True
    values = ([output_types_value] if isinstance(output_types_value, str)
              else list(output_types_value))
    return any(str(v).lower().strip() in _LAVA_CAPABLE for v in values)


def scan_repo(repo: Path, warnings):
    """Parse every artifact file; map func key -> (file, spans, metadata)."""
    files, keys = [], {}
    art_dir = repo / "scripts" / "artifacts"
    for path in sorted(art_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        raw = path.read_bytes()
        newline = "\r\n" if b"\r\n" in raw else "\n"
        source = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        try:
            tree = ast.parse(source)
        except SyntaxError as ex:
            warnings.append(f"{path.name}: syntax error, skipped ({ex})")
            continue
        v2 = None
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", "") == "__artifacts_v2__"
                            for t in node.targets)
                    and isinstance(node.value, ast.Dict)):
                v2 = node.value
                break
        if v2 is None:
            continue
        starts = _line_starts(source)
        artifacts = OrderedDict()
        for key_node, val_node in zip(v2.keys, v2.values):
            func_key = getattr(key_node, "value", None)
            if not isinstance(func_key, str) or not isinstance(val_node,
                                                               ast.Dict):
                continue
            info = _scan_artifact(source, starts, key_node, val_node,
                                  path.name, warnings)
            if info is None:
                continue
            artifacts[func_key] = info
            if func_key in keys:
                warnings.append(
                    f"duplicate artifact key '{func_key}' in {path.name} and "
                    f"{keys[func_key][0].path.name} — both skipped")
                artifacts.pop(func_key)
                keys[func_key][0].artifacts.pop(func_key, None)
                continue
        af = ArtifactFile(path, source, newline, starts, artifacts)
        files.append(af)
        for func_key in artifacts:
            keys[func_key] = (af, artifacts[func_key])
    return files, keys


def _scan_artifact(source, starts, key_node, dict_node, filename, warnings):
    """Spans and metadata for one artifact's dict."""
    # indent of the artifact's metadata entries ("name", "description", ...),
    # i.e. where a "sample_data" key must sit
    if dict_node.keys and dict_node.keys[0] is not None:
        entry_indent = dict_node.keys[0].col_offset
    else:
        entry_indent = key_node.col_offset + 4
    quote = source[_offset(starts, key_node.lineno, key_node.col_offset)]
    if quote not in "\"'":
        quote = '"'
    output_types = None
    sample_span = None      # (key_start, value_start, value_end)
    existing = None
    anchor_end = None       # end offset of artifact_icon's value (preferred)
    last_end = None
    for k_node, v_node in zip(dict_node.keys, dict_node.values):
        k = getattr(k_node, "value", None)
        v_end = _offset(starts, v_node.end_lineno, v_node.end_col_offset)
        last_end = v_end
        if k == "artifact_icon":
            anchor_end = v_end
        elif k == "output_types":
            try:
                output_types = ast.literal_eval(v_node)
            except (ValueError, SyntaxError):
                output_types = None     # non-literal: treat as default
        elif k == "sample_data":
            try:
                existing = ast.literal_eval(v_node)
            except (ValueError, SyntaxError):
                warnings.append(
                    f"{filename}: sample_data is not a literal — skipped")
                return None
            if not isinstance(existing, dict):
                warnings.append(
                    f"{filename}: sample_data is not a dict — skipped")
                return None
            sample_span = (
                _offset(starts, k_node.lineno, k_node.col_offset),
                _offset(starts, v_node.lineno, v_node.col_offset),
                v_end)
    return {
        "entry_indent": entry_indent,
        "quote": quote,
        "lava_capable": _lava_capable(output_types),
        "existing": OrderedDict(existing) if existing else OrderedDict(),
        "sample_span": sample_span,
        "insert_anchor": anchor_end if anchor_end is not None else last_end,
        "dict_end": _offset(starts, dict_node.end_lineno,
                            dict_node.end_col_offset),
    }


# --------------------------------------------------------------------------
# merge + render + rewrite
# --------------------------------------------------------------------------

def merge_sample_data(existing, generated, managed, present, warnings, label):
    """Apply the merge policy; returns the resulting OrderedDict."""
    result = OrderedDict()
    for key, value in existing.items():
        if key in generated:
            if value != generated[key] and not _GENERATED_RE.match(str(value)):
                warnings.append(
                    f"{label}: replacing hand-written text under managed key "
                    f"'{key}' ({value!r}) — move prose to 'notes' to keep it")
            result[key] = generated[key]
        elif key in managed and key in present:
            continue                    # sample re-ran and no longer matches
        else:
            result[key] = value         # unmanaged / absent sample: keep
    for key in sorted(generated):
        if key not in result:
            result[key] = generated[key]
    return result


def _render_str(value, quote):
    escaped = str(value).replace("\\", "\\\\").replace(quote, "\\" + quote)
    return f"{quote}{escaped}{quote}"


def render_value(data, quote, entry_indent):
    """The dict literal for sample_data, one key per line."""
    ind = " " * entry_indent
    inner = " " * (entry_indent + 4)
    lines = ["{"]
    for key, value in data.items():
        lines.append(f"{inner}{_render_str(key, quote)}: "
                     f"{_render_str(value, quote)},")
    lines.append(ind + "}")
    return "\n".join(lines)


def plan_file_edit(af, info, new_data):
    """Queue the span edit for one artifact; returns a change label or None."""
    quote, indent = info["quote"], info["entry_indent"]
    src = af.source
    if info["sample_span"]:
        key_start, value_start, value_end = info["sample_span"]
        if not new_data:
            # remove the whole "sample_data": {...}, entry
            start = src.rfind("\n", 0, key_start) + 1
            if src[start:key_start].strip():
                start = key_start           # key does not start the line
            end = value_end
            tail = src[end:end + 2]
            if tail.startswith(","):
                end += 1
                if src[end:end + 1] == "\n":
                    end += 1
            af.edits.append((start, end, ""))
            return "removed"
        rendered = render_value(new_data, quote, indent)
        if src[value_start:value_end] == rendered:
            return None
        af.edits.append((value_start, value_end, rendered))
        return "updated"
    if not new_data:
        return None
    # insert a new entry after artifact_icon (or the last pair)
    anchor = info["insert_anchor"]
    ind = " " * indent
    block = (f"{_render_str('sample_data', quote)}: "
             f"{render_value(new_data, quote, indent)}")
    after = src[anchor:info["dict_end"]]
    comma = after.lstrip()[:1] == ","
    if comma:
        pos = anchor + after.index(",") + 1
        af.edits.append((pos, pos, f"\n{ind}{block},"))
    else:
        af.edits.append((anchor, anchor, f",\n{ind}{block}"))
    return "added"


def apply_file_edits(af):
    """Apply queued edits bottom-to-top; returns the new file bytes."""
    spans = sorted(af.edits, key=lambda e: e[0], reverse=True)
    for i in range(len(spans) - 1):
        if spans[i + 1][1] > spans[i][0]:
            raise SystemExit(f"error: overlapping edits in {af.path}")
    text = af.source
    for start, end, replacement in spans:
        text = text[:start] + replacement + text[end:]
    if af.newline != "\n":
        text = text.replace("\n", af.newline)
    return text.encode("utf-8")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _run_pylint(repo, files, log=print):
    """(file, symbol) multiset from pylint --disable=C,R; None if unavailable."""
    pylint = shutil.which("pylint")
    cmd = ([pylint] if pylint else [sys.executable, "-m", "pylint"])
    rel = [str(Path(f).relative_to(repo)) for f in files]
    try:
        proc = subprocess.run(
            cmd + ["--disable=C,R", "--output-format=parseable", *rel],
            cwd=repo, env={**os.environ, "PYTHONPATH": "."},
            capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        log(f"validate: pylint unavailable ({ex}) — CI is the backstop")
        return None
    if "No module named pylint" in (proc.stderr or ""):
        log("validate: pylint not installed — CI is the backstop")
        return None
    found = []
    for line in proc.stdout.splitlines():
        m = re.match(r"(.+?):\d+: \[([A-Z]\d+)", line)
        if m:
            found.append((m.group(1), m.group(2)))
    return sorted(found)


def _plugin_count(repo, python, log=print):
    """Plugin count via a PluginLoader subprocess; None when unavailable."""
    code = ("import sys; sys.path.insert(0, '.');"
            "from scripts.plugin_loader import PluginLoader;"
            "print(len(list(PluginLoader().plugins)))")
    try:
        proc = subprocess.run([python, "-c", code], cwd=repo,
                              capture_output=True, text=True, timeout=600,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        log(f"validate: PluginLoader smoke unavailable ({ex})")
        return None
    if proc.returncode != 0:
        log("validate: PluginLoader smoke could not run under "
            f"{python} (missing deps?) — skipping the count check")
        return None
    try:
        return int(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def validate_repo(repo, changed, computed, pre_lint, pre_plugins, python,
                  log=print):
    """Post-edit gates; raises SystemExit on failure."""
    for af in changed:
        source = af.path.read_text(encoding="utf-8")
        tree = ast.parse(source)                     # raises on breakage
        v2 = next(node.value for node in tree.body
                  if isinstance(node, ast.Assign)
                  and any(getattr(t, "id", "") == "__artifacts_v2__"
                          for t in node.targets))
        for key_node, val_node in zip(v2.keys, v2.values):
            func_key = getattr(key_node, "value", None)
            if func_key not in computed:
                continue
            meta = dict(zip(
                (getattr(k, "value", None) for k in val_node.keys),
                val_node.values))
            expected = computed[func_key]
            node = meta.get("sample_data")
            actual = ast.literal_eval(node) if node is not None else None
            if (expected or None) != (actual or None):
                raise SystemExit(
                    f"validate: {af.path.name}::{func_key} sample_data "
                    f"mismatch after edit (expected {expected!r})")
        py_compile.compile(str(af.path), doraise=True)
    log(f"validate: {len(changed)} file(s) re-parsed, verified and compiled")

    if pre_plugins is not None:
        post = _plugin_count(repo, python, log)
        if post != pre_plugins:
            raise SystemExit(
                f"validate: PluginLoader count changed {pre_plugins} -> "
                f"{post} — restoring")
        log(f"validate: PluginLoader OK ({post} plugins)")

    if pre_lint is not None:
        post_lint = _run_pylint(repo, [af.path for af in changed], log)
        if post_lint is not None:
            pre = list(pre_lint)
            new = []
            for item in post_lint:
                if item in pre:
                    pre.remove(item)
                else:
                    new.append(item)
            if new:
                raise SystemExit(
                    "validate: edits introduced pylint warnings: "
                    + ", ".join(f"{f} {s}" for f, s in new))
            if pre_lint:
                log("validate: pre-existing pylint warnings in touched "
                    "files (CI re-lints them; fix with zero-behavior "
                    "pragmas before the PR):")
                for f, s in sorted(set(pre_lint)):
                    log(f"    {f}: {s}")


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def update_repo(repo, tool_norm, sample_facts, managed, present,
                module_filter, apply_mode, python, log=print):
    """Compute and (optionally) apply sample_data for one repo.

    sample_facts: sample_key -> facts (from load_sample_facts)
    present: sample keys whose extraction for this tool was in the DBs
    Returns (changes, warnings).
    """
    warnings = []
    files, keys = scan_repo(repo, warnings)

    # generate entries per artifact — needs the repo scan, because the
    # "0 rows" vs "files found" wording depends on each artifact's
    # output_types (lava-capable or not)
    generated = {}
    unknown = set()
    for sample_key in sorted(sample_facts):
        facts = sample_facts[sample_key]
        for artifact_key, (module, files_matched,
                           _rc) in facts["results"].items():
            if not files_matched:
                continue
            if module_filter and module not in module_filter:
                continue
            if artifact_key not in keys:
                unknown.add(artifact_key)
                continue
            _af, info = keys[artifact_key]
            entry = build_entry(sample_key, facts, artifact_key,
                                info["lava_capable"], tool_norm, warnings)
            if entry is not None:
                generated.setdefault(artifact_key, OrderedDict())[
                    sample_key] = entry
    for artifact_key in sorted(unknown):
        warnings.append(
            f"artifact '{artifact_key}' is in the coverage DB but not in "
            f"{repo.name} — outdated run or renamed artifact; skipped")

    changes, computed_by_file = [], {}
    for af in files:
        computed = {}
        for func_key, info in af.artifacts.items():
            new_data = merge_sample_data(
                info["existing"], generated.get(func_key, {}), managed,
                present, warnings, f"{af.path.name}::{func_key}")
            action = plan_file_edit(af, info, new_data)
            if action:
                changes.append((af, func_key, action, new_data))
            computed[func_key] = new_data
        if af.edits:
            computed_by_file[af] = computed

    changed_files = sorted(computed_by_file, key=lambda a: a.path.name)
    log(f"{repo.name}: {len(changes)} artifact change(s) across "
        f"{len(changed_files)} file(s)")
    for af, func_key, action, new_data in changes:
        log(f"    {action:7s} {af.path.name}::{func_key}")
        for sample_key, value in new_data.items():
            log(f"            {sample_key}: {value}")

    if not apply_mode or not changed_files:
        return changes, warnings

    pre_lint = _run_pylint(repo, [af.path for af in changed_files], log)
    pre_plugins = _plugin_count(repo, python, log)
    originals = {af.path: af.path.read_bytes() for af in changed_files}
    try:
        for af in changed_files:
            af.path.write_bytes(apply_file_edits(af))
        computed = {}
        for af in changed_files:
            computed.update(computed_by_file[af])
        validate_repo(repo, changed_files, computed, pre_lint, pre_plugins,
                      python, log)
    except BaseException:
        for path, blob in originals.items():
            path.write_bytes(blob)
        log(f"{repo.name}: validation failed — original files restored")
        raise
    log(f"{repo.name}: applied and validated {len(changed_files)} file(s)")
    return changes, warnings


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Populate LEAPP artifact sample_data metadata from "
                    "batch-leapp coverage databases.")
    parser.add_argument("--db", action="append", required=True, type=Path,
                        help="batch_apps.sqlite (repeat for several corpora)")
    parser.add_argument("--samples", required=True, type=Path,
                        help="samples.json corpus manifest")
    parser.add_argument("--ileapp", type=Path, help="iLEAPP checkout")
    parser.add_argument("--aleapp", type=Path, help="ALEAPP checkout")
    parser.add_argument("--apply", action="store_true",
                        help="edit the repos (default: dry run)")
    parser.add_argument("--sample", action="append", default=[],
                        help="only process these sample keys")
    parser.add_argument("--module", action="append", default=[],
                        help="only touch these artifact module file stems")
    parser.add_argument("--pick-latest", action="store_true",
                        help="on duplicate matches, use the newest run")
    parser.add_argument("--init-samples", action="store_true",
                        help="register unmatched extractions as stubs "
                             "in samples.json, then exit")
    parser.add_argument("--smoke-python", default=sys.executable,
                        help="python used for the PluginLoader smoke test")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    for db in args.db:
        if not db.is_file():
            print(f"error: not a file: {db}", file=sys.stderr)
            return 2

    samples = load_samples(args.samples)
    extractions = load_extractions(args.db)
    if args.init_samples:
        init_samples(args.samples, samples, extractions)
        return 0
    if not samples:
        print(f"error: {args.samples} has no samples — run --init-samples "
              "or register your extractions first", file=sys.stderr)
        return 2

    repos = {}
    if args.ileapp:
        repos["ileapp"] = args.ileapp.expanduser().resolve()
    if args.aleapp:
        repos["aleapp"] = args.aleapp.expanduser().resolve()
    if not repos:
        print("error: pass --ileapp and/or --aleapp", file=sys.stderr)
        return 2
    for tool_norm, repo in repos.items():
        if not (repo / "scripts" / "artifacts").is_dir():
            print(f"error: {repo} has no scripts/artifacts", file=sys.stderr)
            return 2

    warnings = []
    resolved = match_samples(samples, extractions, args.pick_latest, warnings)
    unmatched_exts = [
        e for e in extractions
        if not any(e is ext for chosen in resolved.values()
                   for ext in chosen.values())]
    for ext in unmatched_exts:
        warnings.append(
            f"extraction '{ext['input_name'] or ext['zip']}' "
            f"({ext['tool']}) is not registered in samples.json — skipped "
            "(use --init-samples)")

    exit_code = 0
    for tool_norm, repo in sorted(repos.items()):
        sample_facts, present = {}, set()
        for sample_key, chosen in resolved.items():
            if args.sample and sample_key not in args.sample:
                continue
            ext = chosen.get(tool_norm)
            if not ext:
                continue
            present.add(sample_key)
            sample_facts[sample_key] = load_sample_facts(
                sample_key, samples[sample_key], ext, warnings)

        changes, repo_warnings = update_repo(
            repo, tool_norm, sample_facts, set(samples), present,
            set(args.module), args.apply, args.smoke_python)
        warnings.extend(repo_warnings)
        if not changes:
            print(f"{repo.name}: nothing to change")

    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for warning in warnings:
            print(f"  - {warning}")
    if not args.apply:
        print("\nDry run — re-run with --apply to edit the repos.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
