#!/usr/bin/env python3
"""
Step 4: Code + Error Extraction.

For each REPRODUCED row:
  1. Locate the victim and polluter source files via os.walk.
  2. git checkout each source file (ensures committed state → reproducibility).
  3. Extract victim method, polluter method, helper methods (@Before/@After etc.),
     and global variables using javalang AST (with regex fallback).
  4. Extract error messages from Surefire XML via BeautifulSoup (with stdout
     regex fallback).
  5. Extract failing lines from the stack trace.
  6. Construct reproduction_steps deterministically.
  7. Write output/row{N}_metadata.json.

Timing fields recorded:
  source_locate_sec  — time to find + git-checkout source files
  error_extract_sec  — time to parse Surefire XML / stdout for error messages
"""

import json
import os
import re
import subprocess
import time

# ── Optional dependencies (graceful fallback if missing) ──────────────────────
try:
    import javalang
    _HAS_JAVALANG = True
except ImportError:
    _HAS_JAVALANG = False

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd, cwd=None, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _write_manifest(pairs, manifest_path):
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"pairs": pairs}, f, indent=2)


def _write_metadata(metadata, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


# ── Source file location ───────────────────────────────────────────────────────

def _fqn_to_path_fragment(fqn):
    """Convert 'com.example.FooTest' → 'com/example/FooTest'."""
    return fqn.replace(".", "/")


def _find_source_file(clone_dir, fqn):
    """
    Locate the .java source file for a fully-qualified class name.

    Algorithm (mirrors flakyDoctor):
      - Derive path fragment from FQN (e.g. 'com/example/FooTest').
      - Walk the repo; match paths that:
          * contain '/test/' (is a test source)
          * do NOT contain '/test-classes/' (not compiled output)
          * end with '{SimpleClassName}.java'
          * contain the full path fragment (handles nested packages)
    Returns the first match, or None.
    """
    simple = fqn.split(".")[-1]
    fragment = _fqn_to_path_fragment(fqn)  # e.g. com/example/FooTest

    for root, _dirs, files in os.walk(clone_dir):
        for fname in files:
            if fname != f"{simple}.java":
                continue
            full = os.path.join(root, fname)
            norm = full.replace(os.sep, "/")
            if "/test/" not in norm:
                continue
            if "/test-classes/" in norm:
                continue
            if fragment in norm:
                return full

    # Looser fallback: just simple class name under /test/
    for root, _dirs, files in os.walk(clone_dir):
        for fname in files:
            if fname != f"{simple}.java":
                continue
            full = os.path.join(root, fname)
            norm = full.replace(os.sep, "/")
            if "/test/" not in norm:
                continue
            if "/test-classes/" in norm:
                continue
            return full

    return None


def _git_checkout_file(clone_dir, file_path):
    """git checkout the file to its committed state."""
    rel = os.path.relpath(file_path, clone_dir)
    _run(f"git checkout -- \"{rel}\"", cwd=clone_dir)


# ── Method extraction — javalang AST ──────────────────────────────────────────

_LIFECYCLE_ANNOTATIONS = {
    "Before", "After", "BeforeClass", "AfterClass",
    "BeforeEach", "AfterEach", "BeforeAll", "AfterAll",
}

_LIFECYCLE_IMPORT_PREFIXES = (
    "org.junit.Before",
    "org.junit.After",
    "org.junit.BeforeClass",
    "org.junit.AfterClass",
    "org.junit.jupiter.api.BeforeEach",
    "org.junit.jupiter.api.AfterEach",
    "org.junit.jupiter.api.BeforeAll",
    "org.junit.jupiter.api.AfterAll",
)


def _extract_method_ast(source_text, method_name):
    """
    Extract one method body using javalang AST.
    Returns the method source as a string, or None on failure.
    """
    if not _HAS_JAVALANG:
        return None
    try:
        tree = javalang.parse.parse(source_text)
        lines = source_text.splitlines()
        for _path, node in tree:
            if not isinstance(node, javalang.tree.MethodDeclaration):
                continue
            if node.name != method_name:
                continue
            start = node.position.line - 1  # 0-indexed
            end   = getattr(node, "end_position", None)
            if end is not None:
                end_line = end.line  # 1-indexed, inclusive
            else:
                # Fallback: brace counting from start line
                end_line = _brace_count_end(lines, start)
            return "\n".join(lines[start:end_line])
        return None
    except Exception:
        return None


def _brace_count_end(lines, start_idx):
    """
    Scan forward from start_idx counting '{' and '}'; return the 1-indexed
    line number where the method's closing brace is reached.
    Fallback when javalang end_position is unavailable.
    """
    depth = 0
    found_open = False
    for i, line in enumerate(lines[start_idx:], start_idx):
        depth += line.count("{") - line.count("}")
        if depth > 0:
            found_open = True
        if found_open and depth <= 0:
            return i + 1  # 1-indexed, inclusive
    return len(lines)


def _extract_method_regex(source_text, method_name):
    """
    Brace-counting fallback for method extraction.
    Matches 'public/protected/private ... methodName(' and counts braces.
    """
    pattern = re.compile(
        r'(?:(?:public|protected|private|static|final|synchronized|void|\w+)\s+)*'
        + re.escape(method_name) + r'\s*\(',
        re.MULTILINE,
    )
    lines = source_text.splitlines()
    for m in pattern.finditer(source_text):
        line_idx = source_text[:m.start()].count("\n")
        end_line = _brace_count_end(lines, line_idx)
        return "\n".join(lines[line_idx:end_line])
    return None


def extract_method(source_text, method_name):
    """Extract a single method — try AST first, then regex fallback."""
    result = _extract_method_ast(source_text, method_name)
    if result:
        return result
    return _extract_method_regex(source_text, method_name) or ""


# ── Helper method extraction ───────────────────────────────────────────────────

_JUNIT3_LIFECYCLE_NAMES = {"setUp", "tearDown"}


def _get_lifecycle_methods(source_text):
    """
    Return list of (annotation_name, method_source) for lifecycle methods.
    Handles:
      - JUnit 4/5 annotation-based lifecycle (@Before, @After, etc.)
      - JUnit 3 convention-based lifecycle (setUp/tearDown in TestCase subclasses)
    Uses javalang if available, otherwise regex.
    """
    results = []
    is_junit3 = "extends TestCase" in source_text or "junit.framework.TestCase" in source_text

    if _HAS_JAVALANG:
        try:
            tree  = javalang.parse.parse(source_text)
            lines = source_text.splitlines()
            found_any = False
            for _path, node in tree:
                if not isinstance(node, javalang.tree.MethodDeclaration):
                    continue
                pos = getattr(node, 'position', None)
                if not pos:
                    continue
                found_any = True

                # Check annotations
                matched_ann = None
                if node.annotations:
                    for ann in node.annotations:
                        if ann.name in _LIFECYCLE_ANNOTATIONS:
                            matched_ann = ann.name
                            break

                # Check JUnit 3 convention names
                if not matched_ann and is_junit3 and node.name in _JUNIT3_LIFECYCLE_NAMES:
                    matched_ann = node.name  # use method name as label

                if not matched_ann:
                    continue

                start = pos.line - 1
                end   = getattr(node, "end_position", None)
                end_line = end.line if end else _brace_count_end(lines, start)
                body = "\n".join(lines[start:end_line])
                # Prepend the annotation line if not already included
                if matched_ann in _LIFECYCLE_ANNOTATIONS and not body.startswith("@"):
                    body = f"@{matched_ann}\n{body}"
                results.append((matched_ann, body))
            if found_any:
                return results
            # AST parsed but no positions — fall through to regex
        except Exception:
            pass

    # Regex fallback — annotation-based
    ann_pattern = re.compile(
        r'(@(?:Before|After|BeforeClass|AfterClass|BeforeEach|AfterEach|BeforeAll|AfterAll)'
        r'(?:\([^)]*\))?)\s*\n\s*((?:(?:public|protected|private|static|void|\w+)\s+)*'
        r'(\w+)\s*\([^)]*\)\s*(?:throws[^{]+)?\{)',
        re.MULTILINE,
    )
    lines = source_text.splitlines()
    for m in ann_pattern.finditer(source_text):
        ann_name  = m.group(1)
        line_idx  = source_text[:m.start()].count("\n")
        end_line  = _brace_count_end(lines, line_idx)
        body      = "\n".join(lines[line_idx:end_line])
        results.append((ann_name, body))

    # Regex fallback — JUnit 3 setUp/tearDown
    if is_junit3:
        junit3_pat = re.compile(
            r'(?:(?:public|protected)\s+)?void\s+(setUp|tearDown)\s*\(\s*\)'
            r'\s*(?:throws[^{]+)?\{',
            re.MULTILINE,
        )
        for m in junit3_pat.finditer(source_text):
            name = m.group(1)
            # Skip if already captured via annotation
            if any(ann == name for ann, _body in results):
                continue
            line_idx = source_text[:m.start()].count("\n")
            end_line = _brace_count_end(lines, line_idx)
            body = "\n".join(lines[line_idx:end_line])
            results.append((name, body))

    return results


def _resolve_superclass_source(source_text, clone_dir):
    """
    If the victim class extends another class, locate and return its source text.
    Returns (superclass_source, superclass_label) or (None, None).
    """
    # Match "class Foo extends Bar" or "class Foo extends com.pkg.Bar"
    extends_pat = re.compile(r'class\s+\w+\s+extends\s+([\w.]+)')
    m = extends_pat.search(source_text)
    if not m:
        return None, None

    super_name = m.group(1)  # e.g. "AbstractLoggingSystemTests" or "com.pkg.Foo"

    # Skip standard base classes that won't have lifecycle methods
    skip = {"Object", "TestCase", "junit.framework.TestCase"}
    if super_name in skip or super_name.split(".")[-1] in skip:
        return None, None

    # If not fully qualified, resolve from imports
    if "." not in super_name:
        import_pat = re.compile(rf'^import\s+([\w.]+\.{re.escape(super_name)})\s*;', re.MULTILINE)
        im = import_pat.search(source_text)
        if im:
            super_fqn = im.group(1)
        else:
            # Same package — try to find by simple name
            super_fqn = super_name
    else:
        super_fqn = super_name

    super_file = _find_source_file(clone_dir, super_fqn)
    if not super_file or not os.path.isfile(super_file):
        return None, None

    with open(super_file, encoding="utf-8", errors="replace") as f:
        return f.read(), super_name


def extract_helper_methods(source_text, clone_dir=None, polluter_source=None):
    """
    Return a '// ---' joined string of all lifecycle helper methods.
    Extracts from:
      1. The victim class source
      2. The victim's superclass (if it extends one and clone_dir is provided)
      3. The polluter class source (if different-class pair)
    """
    parts = []

    # Victim class lifecycle methods
    victim_methods = _get_lifecycle_methods(source_text)
    if victim_methods:
        parts.append("\n// ---\n".join(body for _ann, body in victim_methods))

    # Superclass lifecycle methods
    if clone_dir:
        super_source, super_name = _resolve_superclass_source(source_text, clone_dir)
        if super_source:
            super_methods = _get_lifecycle_methods(super_source)
            if super_methods:
                super_block = "\n// ---\n".join(body for _ann, body in super_methods)
                parts.append(f"// --- SUPERCLASS ({super_name}) ---\n{super_block}")

    # Polluter class lifecycle methods (different-class pairs only)
    if polluter_source and polluter_source != source_text:
        polluter_methods = _get_lifecycle_methods(polluter_source)
        if polluter_methods:
            polluter_block = "\n// ---\n".join(body for _ann, body in polluter_methods)
            parts.append(f"// --- POLLUTER CLASS ---\n{polluter_block}")

    return "\n// ---\n".join(parts)


# ── Global variable extraction ─────────────────────────────────────────────────

def _extract_fields_from_source(source_text):
    """
    Extract all class-level field declarations before the first method.
    Returns a list of (field_name, field_source_line) tuples.
    Uses javalang AST if available, otherwise regex fallback.
    """
    if _HAS_JAVALANG:
        try:
            tree  = javalang.parse.parse(source_text)
            lines = source_text.splitlines()
            fields = []

            # Find the first method start line (to limit field scope)
            first_method_line = len(lines)
            for _path, node in tree:
                if isinstance(node, javalang.tree.MethodDeclaration):
                    pos = getattr(node, 'position', None)
                    if pos:
                        first_method_line = min(first_method_line, pos.line - 1)

            for _path, node in tree:
                if not isinstance(node, javalang.tree.FieldDeclaration):
                    continue
                pos = getattr(node, 'position', None)
                if not pos:
                    continue
                if pos.line - 1 >= first_method_line:
                    continue
                for decl in node.declarators:
                    field_src = lines[pos.line - 1].strip()
                    fields.append((decl.name, field_src))
            if fields:
                return fields
            # AST parsed but found no fields with positions — fall through to regex
        except Exception:
            pass

    # Regex fallback: find lines that look like field declarations
    # before the first annotated method (@Test, @Before, etc.)
    ann_method_pat = re.compile(
        r'@(?:Test|Before|After|BeforeClass|AfterClass|BeforeEach|AfterEach|BeforeAll|AfterAll)\b',
        re.MULTILINE,
    )
    first_ann = ann_method_pat.search(source_text)
    if first_ann:
        header = source_text[:first_ann.start()]
    else:
        header = "\n".join(source_text.splitlines()[:100])

    field_pat = re.compile(
        r'^\s*(?:(?:private|protected|public|static|final|volatile|transient)\s+)*'
        r'[\w<>\[\]]+\s+(\w+)\s*(?:=\s*[^;]+)?;',
        re.MULTILINE,
    )
    fields = []
    for m in field_pat.finditer(header):
        name = m.group(1)
        line = m.group(0).strip()
        fields.append((name, line))
    return fields


def extract_global_variables(victim_source, polluter_source=None):
    """
    Extract all class-level field declarations before the first method
    from the victim class (and polluter class, if different).

    Includes all fields — no filtering by reference in method bodies.
    Deduplicates by field name.
    """
    seen = {}  # field_name -> field_source_line

    for name, line in _extract_fields_from_source(victim_source):
        if name not in seen:
            seen[name] = line

    if polluter_source and polluter_source != victim_source:
        for name, line in _extract_fields_from_source(polluter_source):
            if name not in seen:
                seen[name] = line

    return "\n".join(seen.values())


# ── Error message extraction ───────────────────────────────────────────────────

def _find_surefire_reports_dir(clone_dir, package):
    """Locate the surefire-reports directory for the given module."""
    if package not in (".", ""):
        candidate = os.path.join(clone_dir, package, "target", "surefire-reports")
    else:
        candidate = os.path.join(clone_dir, "target", "surefire-reports")
    if os.path.isdir(candidate):
        return candidate

    # Walk looking for any surefire-reports dir
    for root, dirs, _files in os.walk(clone_dir):
        if "surefire-reports" in dirs:
            return os.path.join(root, "surefire-reports")
    return None


def _msgs_from_xml(xml_path):
    """Parse a single Surefire XML and return failure/error message strings."""
    if not _HAS_BS4:
        return []
    msgs = []
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as f:
            soup = BeautifulSoup(f, "lxml-xml")
        for tag in soup.find_all(["failure", "error"]):
            msg = tag.get("message", "")
            typ = tag.get("type", "")
            if not msg:
                # Fall back to exception type (à la FlakyDoctor) rather than
                # dumping the raw stack trace via get_text().
                msg = typ
            # Strip [REPRODUCED] prefix injected by our programmatic reproducer
            # Handles both "[REPRODUCED] row27: msg" and "[REPRODUCED] msg" forms
            msg = re.sub(r'^\[REPRODUCED\]\s*(?:\S+:\s+)?', '', msg)
            msg = re.sub(r'\s+', ' ', msg).strip()
            if msg and msg not in msgs:
                msgs.append(msg)
    except Exception:
        pass
    return msgs


def _extract_errors_xml(surefire_dir, victim_class, polluter_class):
    """
    Fallback XML scan when the exact running-class XML lookup misses.
    Checks XMLs whose filename contains the victim or polluter simple class name.
    """
    simple_victim   = victim_class.split(".")[-1]
    simple_polluter = polluter_class.split(".")[-1]
    messages = []
    for fname in os.listdir(surefire_dir):
        if not fname.endswith(".xml"):
            continue
        if simple_victim in fname or simple_polluter in fname:
            xml_path = os.path.join(surefire_dir, fname)
            messages.extend(m for m in _msgs_from_xml(xml_path) if m not in messages)
    return messages


def _extract_errors_stdout(log_text, victim_class):
    """
    Regex fallback: extract assertion/exception messages from Maven stdout.
    Matches Java exception/assertion lines; skips Maven infrastructure warnings.
    """
    # Patterns that indicate a genuine test failure line
    patterns = [
        re.compile(r'expected:<.+?> but was:<.+?>', re.IGNORECASE),
        re.compile(r'AssertionError(?:: .+)?', re.IGNORECASE),
        re.compile(r'(?:java\.lang|org\.junit|junit\.framework)\.\w+(?:Error|Exception|Failure): .+'),
    ]
    # Lines to skip that are Maven infrastructure noise, not test failures
    skip_phrases = [
        "failed to transfer",
        "maven-default-http-blocker",
        "Could not transfer",
        "Blocked mirror",
        "BUILD FAILURE",
        "BUILD SUCCESS",
    ]
    messages = []
    for line in log_text.splitlines():
        # Skip obvious Maven noise
        if any(phrase in line for phrase in skip_phrases):
            continue
        for pat in patterns:
            m = pat.search(line)
            if m:
                msg = m.group(0).strip()
                if msg and msg not in messages:
                    messages.append(msg)
                break
    return messages


def _running_class_from_log(log_text):
    """
    Parse Maven surefire output for 'Running {classname}' lines.
    Surefire 2.x prints these inside the T E S T S block without [INFO] prefix;
    older forks may include [INFO].  Match both forms.
    Returns a list of class names that actually ran in this invocation.
    """
    pat = re.compile(r'^(?:\[INFO\]\s+)?Running\s+([\w.$]+)', re.MULTILINE)
    return pat.findall(log_text)


def extract_error_messages(clone_dir, package, victim_class, polluter_class, phase_b_log):
    """
    Return error message string for the victim test failure.

    XML strategy (preferred — never stale):
      1. Parse phase_b_log for '[INFO] Running {classname}' to get the exact
         class(es) that ran. Look only at TEST-{classname}.xml files — these are
         always overwritten by the current run, so they can't be from a prior run.
      2. Fall back to victim/polluter class-name matching in surefire-reports.

    Stdout fallback: regex against the phase B log itself.
    """
    log_text = ""
    if phase_b_log and os.path.isfile(phase_b_log):
        with open(phase_b_log, encoding="utf-8", errors="replace") as f:
            log_text = f.read()

    surefire_dir = _find_surefire_reports_dir(clone_dir, package)

    if surefire_dir:
        # Pass 1: XMLs for classes we know ran in this Phase B invocation
        running_classes = _running_class_from_log(log_text)
        msgs = []
        for cls in running_classes:
            xml_path = os.path.join(surefire_dir, f"TEST-{cls}.xml")
            if os.path.isfile(xml_path):
                msgs.extend(m for m in _msgs_from_xml(xml_path) if m not in msgs)
        if msgs:
            return "\n".join(msgs)

        # Pass 2: victim/polluter class-name matching
        msgs = _extract_errors_xml(surefire_dir, victim_class, polluter_class)
        if msgs:
            return "\n".join(msgs)

    # Stdout fallback
    if log_text:
        msgs = _extract_errors_stdout(log_text, victim_class)
        if msgs:
            return "\n".join(msgs[:5])

    return ""


# ── Failing line extraction ────────────────────────────────────────────────────

_ASSERT_PAT = re.compile(
    r'\b(assert(?:That|Equals|True|False|NotNull|Null|Same|ArrayEquals|Throws|Fail)'
    r'|verify\s*\('
    r'|verifyNoMore|verifyZero'
    r'|fail\s*\()',
    re.IGNORECASE,
)


def extract_failing_lines(phase_b_log, victim_class, source_text, victim_body=None):
    """
    Extract the source line(s) referenced in the victim's stack trace.

    Primary: match stack-trace frames like (VictimClass.java:N) in the log and
    return the corresponding source lines.

    Fallback: when the primary approach finds nothing (e.g. the programmatic
    reproducer runs the victim via reflection so Surefire only records the
    invoke() call-site, not the original frame), scan the victim method body
    for assertion/verify lines and return all of them.
    """
    if not phase_b_log or not os.path.isfile(phase_b_log):
        return ""

    simple  = victim_class.split(".")[-1]
    lines   = source_text.splitlines() if source_text else []

    with open(phase_b_log, encoding="utf-8", errors="replace") as f:
        log_text = f.read()

    # Primary: stack-trace line numbers
    pat = re.compile(rf'\({re.escape(simple)}\.java:(\d+)\)')
    found = []
    for m in pat.finditer(log_text):
        lineno = int(m.group(1)) - 1  # 0-indexed
        if 0 <= lineno < len(lines):
            src_line = lines[lineno].strip()
            if src_line and src_line not in found:
                found.append(src_line)

    if found:
        return "\n".join(found)

    # Fallback: scan only the victim method body for assertion/verify lines.
    # Using victim_body (not the full source file) avoids picking up assertions
    # from other methods in the same class.
    body = victim_body or ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and _ASSERT_PAT.search(stripped) and stripped not in found:
            found.append(stripped)

    return "\n".join(found)


# ── Reproduction steps ─────────────────────────────────────────────────────────

def build_reproduction_steps(pair):
    """
    Return a single descriptive string summarising the confirmed OD relationship.

    Only states empirically verified facts (Phase A/B results) plus the
    structural pair type from the CSV. No git/Maven commands, no inferred
    pollution mechanism.
    """
    victim_method   = pair.get("victim_method", "")
    polluter_method = pair.get("polluter_method", "")
    same_class      = pair.get("same_class", False)
    pair_type       = "Same-class pair." if same_class else "Different-class pair."

    # Include the JDK version used for reproduction
    java_ver = pair.get("required_java_version") or 8
    from config import java_home_for
    _, actual_ver, _ = java_home_for(java_ver)
    jdk_label = f"JDK {actual_ver}" if actual_ver else f"JDK {java_ver}"

    return (
        f"{victim_method} passes when run alone but fails when run immediately "
        f"after {polluter_method} in the same JVM process ({jdk_label}, no restart "
        f"between them). {pair_type}"
    )


# ── Process one pair ───────────────────────────────────────────────────────────

def _process_pair(pair, output_dir):
    """
    Extract all required fields for one REPRODUCED pair.
    Returns metadata dict.
    """
    clone_dir     = pair["clone_dir"]
    package       = pair["package"]
    victim_fqn    = pair["victim_class"]
    polluter_fqn  = pair["polluter_class"]
    victim_meth   = pair["victim_method"]
    polluter_meth = pair["polluter_method"]
    phase_b_log   = pair.get("phase_b_log")

    # ── Locate + checkout source files ────────────────────────────────────────
    t_locate = time.time()

    victim_file = _find_source_file(clone_dir, victim_fqn)
    if victim_file:
        _git_checkout_file(clone_dir, victim_file)
    else:
        print(f"  WARNING: victim source file not found for {victim_fqn}")

    same_class = pair.get("same_class", False)
    if same_class or victim_fqn == polluter_fqn:
        polluter_file = victim_file
    else:
        polluter_file = _find_source_file(clone_dir, polluter_fqn)
        if polluter_file and polluter_file != victim_file:
            _git_checkout_file(clone_dir, polluter_file)

    source_locate_sec = time.time() - t_locate

    # Read source
    victim_source   = ""
    polluter_source = ""
    if victim_file and os.path.isfile(victim_file):
        with open(victim_file, encoding="utf-8", errors="replace") as f:
            victim_source = f.read()
    if polluter_file and os.path.isfile(polluter_file):
        with open(polluter_file, encoding="utf-8", errors="replace") as f:
            polluter_source = f.read()

    # ── Method extraction ──────────────────────────────────────────────────────
    victim_body   = extract_method(victim_source, victim_meth)
    polluter_body = extract_method(polluter_source, polluter_meth)

    victim_label   = f"// VICTIM: {pair.get('victim', victim_fqn + '#' + victim_meth)}"
    polluter_label = f"// POLLUTER: {pair.get('polluter', polluter_fqn + '#' + polluter_meth)}"
    labeled_victim   = f"{victim_label}\n{victim_body}"   if victim_body   else ""
    labeled_polluter = f"{polluter_label}\n{polluter_body}" if polluter_body else ""
    full_test_code = "\n\n".join(filter(None, [labeled_victim, labeled_polluter]))

    # ── Lifecycle helpers (victim + superclass + polluter) ───────────────────────
    helper_methods = extract_helper_methods(victim_source, clone_dir, polluter_source)

    # ── Global variables ───────────────────────────────────────────────────────
    global_variables = extract_global_variables(
        victim_source, polluter_source
    )

    # ── Error messages ─────────────────────────────────────────────────────────
    t_error = time.time()
    error_messages = extract_error_messages(
        clone_dir, package, victim_fqn, polluter_fqn, phase_b_log
    )
    error_extract_sec = time.time() - t_error

    # ── Failing lines ──────────────────────────────────────────────────────────
    failing_lines = extract_failing_lines(phase_b_log, victim_fqn, victim_source, victim_body)

    # ── Reproduction steps ─────────────────────────────────────────────────────
    reproduction_steps = build_reproduction_steps(pair)

    return {
        "od_or_id": pair.get("od_or_id", "OD"),
        "source": pair.get("repo_url", ""),
        "reproduction_steps": reproduction_steps,
        "victim_test_name": pair.get("victim", ""),
        "polluter_test_name": pair.get("polluter", ""),
        "error_messages": error_messages,
        "failing_lines": failing_lines,
        "global_variables": global_variables,
        "helper_methods": helper_methods,
        "full_test_code": full_test_code,
        # Timing (stored in metadata, used by step5 for timing_report)
        "_source_locate_sec": source_locate_sec,
        "_error_extract_sec": error_extract_sec,
    }


# ── Main step function ─────────────────────────────────────────────────────────

def run_step4(pairs, manifest_path, output_dir, all_pairs=None):
    """
    Extract code and error information for all REPRODUCED rows.
    Writes per-row metadata JSON files. Updates pairs with timing.

    all_pairs: if provided, used for manifest writes so the full dataset is
               preserved when pairs is a filtered subset (partial run).
    """
    manifest_pairs = all_pairs if all_pairs is not None else pairs
    print("\n" + "=" * 70)
    print("STEP 4: Code + Error Extraction")
    print("=" * 70)

    reproduced = [p for p in pairs if p.get("step3_status") == "REPRODUCED"]
    print(f"{len(reproduced)} REPRODUCED pairs to extract\n")

    for idx, pair in enumerate(reproduced, 1):
        row_key = pair["row_key"]
        print(f"ROW {idx}/{len(reproduced)}: {row_key}  {pair['victim_method']}")

        meta_path = os.path.join(output_dir, f"{row_key}_metadata.json")

        # Resumable — skip if metadata already written
        if os.path.isfile(meta_path):
            print(f"  SKIP (metadata already exists)")
            continue

        try:
            metadata = _process_pair(pair, output_dir)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            metadata = None

        if metadata:
            pair["source_locate_sec"] = metadata.pop("_source_locate_sec", 0.0)
            pair["error_extract_sec"] = metadata.pop("_error_extract_sec", 0.0)
            _write_metadata(metadata, meta_path)
            print(f"  Written: {os.path.basename(meta_path)}")
        else:
            pair["source_locate_sec"] = 0.0
            pair["error_extract_sec"] = 0.0

        _write_manifest(manifest_pairs, manifest_path)

    print(f"\nSTEP 4 COMPLETE — metadata files in {output_dir}")
    return pairs


# ── Standalone runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    HERE = os.path.dirname(os.path.abspath(__file__))
    _out = os.path.join(HERE, "output")

    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=_out)
    args = ap.parse_args()

    manifest_path = os.path.join(args.output_dir, "manifest.json")

    with open(manifest_path) as f:
        manifest = json.load(f)
    pairs = manifest["pairs"]

    run_step4(pairs, manifest_path, args.output_dir)
