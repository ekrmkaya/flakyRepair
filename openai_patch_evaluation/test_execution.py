#!/usr/bin/env python3
"""
test_execution.py — OD test execution module for the patch evaluation pipeline.

Provides:
  - load_targets()         Read manifest from data collection, derive target metadata
  - run_cmd()              Robust command runner with marker-polling + process group kill
  - run_od_test()          Unified OD test runner dispatching to the correct strategy
  - run_victim_alone()     Run victim test in isolation
  - run_polluter_alone()   Run polluter test in isolation
  - interpret_for_patch()  Map reproduction result to patch evaluation result
  - make_env()             Build JDK environment dict
  - SKIP_FLAGS             Maven skip flags

Adapted from failure_data_collection/step3_reproduce.py and config.py.
All reads stay within the repository root.
"""

import json
import os
import re
import signal
import subprocess
import textwrap
import time


# =============================================================================
# Paths
# =============================================================================

_THIS_DIR     = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT    = os.path.dirname(_THIS_DIR)                    # repository root
_MANIFEST     = os.path.join(_REPO_ROOT, "failure_data_collection", "output", "manifest.json")
_REPOS_DIR    = os.path.join(_REPO_ROOT, "repos")


# =============================================================================
# Config (adapted from failure_data_collection/config.py)
# =============================================================================

_JAVA_FALLBACKS = {
    8:  "/Library/Java/JavaVirtualMachines/adoptopenjdk-8.jdk/Contents/Home",
    11: "/opt/homebrew/Cellar/openjdk@11/11.0.30/libexec/openjdk.jdk/Contents/Home",
    17: "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    21: "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
}

_JAVA_ENV_VARS = {8: "JAVA8_HOME", 11: "JAVA11_HOME", 17: "JAVA17_HOME", 21: "JAVA21_HOME"}


def _build_java_homes():
    homes = {}
    for ver, env_var in _JAVA_ENV_VARS.items():
        path = os.environ.get(env_var, _JAVA_FALLBACKS.get(ver, ""))
        if path and os.path.isfile(os.path.join(path, "bin", "javac")):
            homes[ver] = path
    return homes


_JAVA_HOMES = _build_java_homes()


def java_home_for(required_version):
    v = required_version or 8
    if v in _JAVA_HOMES:
        return _JAVA_HOMES[v], v, None
    for fallback in sorted(_JAVA_HOMES.keys()):
        if fallback >= v:
            return _JAVA_HOMES[fallback], fallback, f"Java {v} not found; using Java {fallback}"
    return None, None, f"No suitable JDK for Java {v}. Available: {sorted(_JAVA_HOMES.keys())}"


def make_env(java_version=8):
    java_home, _, note = java_home_for(java_version)
    if java_home is None:
        java_home = _JAVA_FALLBACKS.get(8, "")
    env = os.environ.copy()
    env["JAVA_HOME"] = java_home
    env["PATH"] = os.path.join(java_home, "bin") + ":" + env.get("PATH", "")
    return env


SKIP_FLAGS = (
    "-Drat.skip=true "
    "-Dcheckstyle.skip=true "
    "-Ddisable.checks=true "
    "-Denforcer.skip=true "
    "-Dspotbugs.skip=true "
    "-Djacoco.skip=true "
    "-Danimal.sniffer.skip=true "
    "-Dmaven.antrun.skip=true "
    "-Dspotless.check.skip=true "
    "-Dspring-javaformat.skip=true "
    "-Dmaven.test.failure.ignore=true "
    "-Djacoco.agent.argLine= "
    "-DargLine= "
    "-Dmaven.plugin.skip=true "
    "-Dlicense.skip=true "
    "-DtrimStackTrace=false "
)

COMPILE_TIMEOUT = 600
PHASE_B_COMPILE_TIMEOUT = 1200
DEFAULT_SUREFIRE_TIMEOUT = 360

TIMEOUT_OVERRIDES = {
    "undertow":                  600,
    "spring-boot":               900,
    "flow":                      600,
    "openhtmltopdf":             600,
    "shardingsphere-elasticjob": 600,
    "http-request":              600,
}

STRATEGY_OVERRIDES = {
    "undertow":    "custom_undertow",
    "spring-boot": "plus_syntax",
}

OFFLINE_REPOS = {
    "http-request", "openhtmltopdf", "shardingsphere-elasticjob", "cukes",
    "Sentinel", "Universal-G-Code-Sender", "aismessages", "dropwizard",
    "fastjson", "jnr-posix", "marine-api", "undertow", "visualee",
    "wikidata-toolkit",
}

TEST_SOURCE_DIR_OVERRIDES = {"Universal-G-Code-Sender": "test"}

SUREFIRE_PRE_PHASES = {"Sentinel": "initialize"}


def surefire_timeout_for(repo_slug):
    return TIMEOUT_OVERRIDES.get(repo_slug, DEFAULT_SUREFIRE_TIMEOUT)


def strategy_for(repo_slug, same_class):
    if repo_slug in STRATEGY_OVERRIDES:
        return STRATEGY_OVERRIDES[repo_slug]
    return "programmatic_same_class" if same_class else "programmatic_diff_class"


def offline_flag_for(repo_slug):
    return "--offline" if repo_slug in OFFLINE_REPOS else ""


def test_source_root_for(repo_slug, default="src/test/java"):
    return TEST_SOURCE_DIR_OVERRIDES.get(repo_slug, default)


def surefire_pre_phase_for(repo_slug):
    return SUREFIRE_PRE_PHASES.get(repo_slug, "")


# =============================================================================
# Target loading from manifest
# =============================================================================

def _build_entry_index():
    """Build a lookup from victim_test_name to flaky_test_data entry.

    The flaky_test_data.json array order may differ from the manifest row
    numbering.  This index allows O(1) lookup by victim name.
    """
    data_path = os.path.join(_REPO_ROOT, "failure_data_collection", "output", "flaky_test_data.json")
    if not os.path.isfile(data_path):
        return {}
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    return {e["victim_test_name"]: e for e in data.get("testdata", [])}


def load_entry_for_target(target):
    """Load the flaky_test_data entry matching a target, by victim name."""
    index = _build_entry_index()
    victim = target.get("victim", "")
    entry = index.get(victim)
    if entry is None:
        raise KeyError(f"No flaky_test_data entry for victim: {victim}")
    return entry


def load_targets():
    """Read the data collection manifest and derive target metadata.

    Returns dict keyed by row_num, matching the interface sections 4/5/6/7 expect.
    """
    if not os.path.isfile(_MANIFEST):
        raise FileNotFoundError(f"Manifest not found: {_MANIFEST}")

    with open(_MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)

    target_map = {}
    for pair in manifest["pairs"]:
        row_num    = pair["row_num"]
        repo_slug  = pair["repo_slug"]
        clone_dir  = pair["clone_dir"]
        package    = pair["package"]

        # Derive repo_local_path (relative to the repos parent dir)
        repo_local_path = os.path.relpath(clone_dir, _REPO_ROOT)

        # Derive maven_module_path from resolved_module_path (strip trailing /pom.xml)
        resolved = pair.get("resolved_module_path", "")
        if resolved.endswith("/pom.xml"):
            maven_module_abs = resolved[:-len("/pom.xml")]
        elif resolved.endswith("\\pom.xml"):
            maven_module_abs = resolved[:-len("\\pom.xml")]
        else:
            maven_module_abs = os.path.join(clone_dir, package) if package not in (".", "") else clone_dir
        maven_module_path = os.path.relpath(maven_module_abs, _REPO_ROOT)

        # Derive test_src_path from victim_class
        victim_class = pair["victim_class"]
        class_rel    = victim_class.replace(".", os.sep) + ".java"
        test_src_dir = test_source_root_for(repo_slug)
        if package not in (".", ""):
            test_src_abs = os.path.join(clone_dir, package, test_src_dir, class_rel)
        else:
            test_src_abs = os.path.join(clone_dir, test_src_dir, class_rel)
        test_src_path = os.path.relpath(test_src_abs, _REPO_ROOT)

        same_class = pair.get("same_class", pair["victim_class"] == pair["polluter_class"])

        target_map[row_num] = {
            "test_id":                f"row{row_num:02d}_{repo_slug}",
            "row_num":                row_num,
            "repo_url":               pair.get("repo_url", ""),
            "repo_slug":              repo_slug,
            "commit":                 pair.get("commit_hash", ""),
            "package":                package,
            "victim":                 pair["victim"],
            "victim_class":           pair["victim_class"],
            "victim_method":          pair["victim_method"],
            "polluter":               pair["polluter"],
            "polluter_class":         pair["polluter_class"],
            "polluter_method":        pair["polluter_method"],
            "same_class":             same_class,
            "repo_local_path":        repo_local_path,
            "test_src_path":          test_src_path,
            "test_src_found":         os.path.isfile(test_src_abs),
            "maven_module_path":      maven_module_path,
            "reproduction_strategy":  strategy_for(repo_slug, same_class),
            "clone_dir":              clone_dir,
            "mvn_cmd":                pair.get("mvn_cmd", "mvn"),
            "required_java_version":  pair.get("required_java_version", 8),
        }

    return target_map


# =============================================================================
# Robust command runner (from step3_reproduce.py)
# =============================================================================

def _kill_proc(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


_DONE_MARKERS = ("Tests run:", "BUILD SUCCESS", "BUILD FAILURE",
                 "No tests were run", "No tests matching")
_POLL_SEC  = 2
_GRACE_SEC = 5


def run_cmd(cmd, cwd, timeout, log_path, env=None):
    """Run a shell command with marker-polling and process group management.

    Returns (exit_code, output_string, elapsed_seconds).
    """
    if env is None:
        env = make_env()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    t0 = time.time()
    try:
        with open(log_path, "w", encoding="utf-8") as log_f:
            proc = subprocess.Popen(
                cmd, shell=True,
                stdout=log_f, stderr=subprocess.STDOUT,
                cwd=cwd, env=env,
                start_new_session=True,
            )

        deadline = time.time() + timeout
        done_detected_at = None

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                _kill_proc(proc)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\nTIMEOUT after {timeout}s\n")
                break

            try:
                proc.wait(timeout=min(_POLL_SEC, remaining))
                break
            except subprocess.TimeoutExpired:
                pass

            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if any(m in content for m in _DONE_MARKERS):
                    if done_detected_at is None:
                        done_detected_at = time.time()
                    elif time.time() - done_detected_at >= _GRACE_SEC:
                        _kill_proc(proc)
                        break
            except (FileNotFoundError, IOError):
                pass

    except Exception as exc:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"ERROR launching command: {exc}\n")
        elapsed = round(time.time() - t0, 2)
        return 1, f"ERROR launching command: {exc}\n", elapsed

    with open(log_path, "r", encoding="utf-8") as f:
        output = f.read()
    rc = proc.returncode if proc.returncode is not None else 1
    elapsed = round(time.time() - t0, 2)
    return rc, output, elapsed


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass


# =============================================================================
# Java reproducer templates (from step3_reproduce.py)
# =============================================================================

_SAME_CLASS_TEMPLATE = textwrap.dedent("""\
package flakyrepro;

import org.junit.Test;
import java.lang.reflect.*;
import java.util.*;

public class ProgrammaticReproducer_{SAFE_KEY} {{

    @SuppressWarnings("unchecked")
    private static <A extends java.lang.annotation.Annotation>
    List<Method> getAnnotated(Class<?> cls, Class<A> ann) {{
        List<Method> result = new ArrayList<Method>();
        for (Class<?> c = cls; c != null && c != Object.class; c = c.getSuperclass()) {{
            for (Method m : c.getDeclaredMethods()) {{
                if (m.isAnnotationPresent(ann)) {{
                    m.setAccessible(true);
                    result.add(m);
                }}
            }}
        }}
        return result;
    }}

    @Test
    public void reproduce() throws Exception {{
        Class<?> cls = Class.forName("{VICTIM_CLASS}");
        Object inst = cls.getDeclaredConstructor().newInstance();

        for (Method m : getAnnotated(cls, org.junit.BeforeClass.class)) m.invoke(null);

        // --- POLLUTER ---
        for (Method m : getAnnotated(cls, org.junit.Before.class))  m.invoke(inst);
        try {{
            cls.getMethod("{POLLUTER_METHOD}").invoke(inst);
        }} catch (java.lang.reflect.InvocationTargetException __) {{
        }} finally {{
            for (Method m : getAnnotated(cls, org.junit.After.class)) m.invoke(inst);
        }}

        // --- VICTIM ---
        for (Method m : getAnnotated(cls, org.junit.Before.class))  m.invoke(inst);
        try {{
            cls.getMethod("{VICTIM_METHOD}").invoke(inst);
        }} catch (java.lang.reflect.InvocationTargetException e) {{
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            System.out.println("[REPRODUCED] {SAFE_KEY}: "
                + cause.getClass().getSimpleName() + ": " + cause.getMessage());
            if (cause instanceof AssertionError) throw (AssertionError) cause;
            if (cause instanceof RuntimeException) throw (RuntimeException) cause;
            throw new AssertionError("[REPRODUCED] " + cause.getMessage(), cause);
        }} finally {{
            for (Method m : getAnnotated(cls, org.junit.After.class))      m.invoke(inst);
            for (Method m : getAnnotated(cls, org.junit.AfterClass.class)) m.invoke(null);
        }}
    }}
}}
""")

_DIFF_CLASS_TEMPLATE = textwrap.dedent("""\
package flakyrepro;

import org.junit.Test;
import java.lang.reflect.*;
import java.util.*;

public class DiffClassReproducer_{SAFE_KEY} {{

    @SuppressWarnings("unchecked")
    private static <A extends java.lang.annotation.Annotation>
    List<Method> getAnnotated(Class<?> cls, Class<A> ann) {{
        List<Method> result = new ArrayList<Method>();
        for (Class<?> c = cls; c != null && c != Object.class; c = c.getSuperclass()) {{
            for (Method m : c.getDeclaredMethods()) {{
                if (m.isAnnotationPresent(ann)) {{
                    m.setAccessible(true);
                    result.add(m);
                }}
            }}
        }}
        return result;
    }}

    @Test
    public void reproduce() throws Exception {{
        // -- POLLUTER CLASS ---
        Class<?> polCls  = Class.forName("{POLLUTER_CLASS}");
        Object   polInst = polCls.getDeclaredConstructor().newInstance();
        for (Method m : getAnnotated(polCls, org.junit.BeforeClass.class)) m.invoke(null);
        for (Method m : getAnnotated(polCls, org.junit.Before.class))      m.invoke(polInst);
        polCls.getMethod("{POLLUTER_METHOD}").invoke(polInst);
        for (Method m : getAnnotated(polCls, org.junit.After.class))       m.invoke(polInst);
        for (Method m : getAnnotated(polCls, org.junit.AfterClass.class))  m.invoke(null);

        // -- VICTIM CLASS ---
        Class<?> vicCls = Class.forName("{VICTIM_CLASS}");
        try {{
            Object vicInst = vicCls.getDeclaredConstructor().newInstance();
            for (Method m : getAnnotated(vicCls, org.junit.BeforeClass.class)) m.invoke(null);
            for (Method m : getAnnotated(vicCls, org.junit.Before.class))      m.invoke(vicInst);
            try {{
                vicCls.getMethod("{VICTIM_METHOD}").invoke(vicInst);
            }} finally {{
                for (Method m : getAnnotated(vicCls, org.junit.After.class))      m.invoke(vicInst);
                for (Method m : getAnnotated(vicCls, org.junit.AfterClass.class)) m.invoke(null);
            }}
        }} catch (java.lang.reflect.InvocationTargetException e) {{
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            System.out.println("[REPRODUCED] {SAFE_KEY}: "
                + cause.getClass().getSimpleName() + ": " + cause.getMessage());
            if (cause instanceof AssertionError) throw (AssertionError) cause;
            if (cause instanceof RuntimeException) throw (RuntimeException) cause;
            throw new AssertionError("[REPRODUCED] " + cause.getMessage(), cause);
        }}
    }}
}}
""")

_UNDERTOW_RUNNER_TEMPLATE = textwrap.dedent("""\
package flakyrepro;
import io.undertow.testutils.DefaultServer;
import org.junit.runners.model.FrameworkMethod;
import org.junit.runners.model.InitializationError;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

public class UndertowRunner_{SAFE_KEY} extends DefaultServer {{
    private static final List<String> ORDER = Arrays.asList("{POLLUTER_METHOD}", "{VICTIM_METHOD}");

    public UndertowRunner_{SAFE_KEY}(Class<?> klass) throws InitializationError {{
        super(klass);
    }}

    @Override
    protected List<FrameworkMethod> computeTestMethods() {{
        List<FrameworkMethod> list = new ArrayList<FrameworkMethod>(super.computeTestMethods());
        Collections.sort(list, (a, b) -> {{
            int ai = ORDER.indexOf(a.getName());
            int bi = ORDER.indexOf(b.getName());
            if (ai < 0) ai = ORDER.size();
            if (bi < 0) bi = ORDER.size();
            if (ai != bi) return Integer.compare(ai, bi);
            return a.getName().compareTo(b.getName());
        }});
        return list;
    }}
}}
""")

_UNDERTOW_SUITE_TEMPLATE = textwrap.dedent("""\
package flakyrepro;
import org.junit.runner.RunWith;

@RunWith(UndertowRunner_{SAFE_KEY}.class)
public class UndertowSuite_{SAFE_KEY} extends {VICTIM_CLASS} {{
}}
""")


# =============================================================================
# Result parsing
# =============================================================================

def parse_repro_result(output, victim_method, polluter_method):
    """Classify the outcome of a polluter+victim reproducer run."""
    if "TIMEOUT after" in output:
        return "TIMEOUT"
    if "No tests were run" in output or "No tests matching" in output:
        return "TESTS_NOT_FOUND"
    if not re.search(r"Tests run:", output):
        return "BUILD_ERROR"

    victim_failed = bool(
        re.search(rf"{re.escape(victim_method)}.*FAILURE", output) or
        re.search(rf"FAILURE.*{re.escape(victim_method)}", output) or
        re.search(rf"{re.escape(victim_method)}.*<<<\s*FAILURE", output) or
        "[REPRODUCED]" in output
    )
    polluter_failed = bool(
        re.search(rf"{re.escape(polluter_method)}.*FAILURE", output) or
        re.search(rf"FAILURE.*{re.escape(polluter_method)}", output)
    )

    if victim_failed and not polluter_failed:
        return "REPRODUCED"
    if not victim_failed:
        return "NOT_REPRODUCED:VICTIM_PASSES_WITH_POLLUTER"
    if polluter_failed:
        return "NOT_REPRODUCED:POLLUTER_ALSO_FAILED"
    return "NOT_REPRODUCED:UNKNOWN"


def parse_single_test_result(output, method_name):
    """Classify the result of running a single test method."""
    if "TIMEOUT after" in output:
        return "BUILD_ERROR"
    if not re.search(r"Tests run:", output):
        return "BUILD_ERROR"
    if "No tests were run" in output:
        return "TESTS_NOT_FOUND"

    failed = bool(
        re.search(rf"{re.escape(method_name)}.*FAILURE", output) or
        re.search(rf"FAILURE.*{re.escape(method_name)}", output) or
        re.search(r"Tests run:.*Failures: [1-9]", output) or
        re.search(r"Tests run:.*Errors: [1-9]", output)
    )
    return "FAILED" if failed else "PASSED"


def interpret_for_patch(repro_status):
    """Map reproduction status to patch evaluation status.

    In patch evaluation context:
      - If victim PASSES with polluter -> patch fixed the flakiness
      - If victim FAILS with polluter  -> patch did not fix it
    """
    if repro_status == "NOT_REPRODUCED:VICTIM_PASSES_WITH_POLLUTER":
        return "PASSED"
    if repro_status.startswith("REPRODUCED"):
        return "FAILED"
    if repro_status in ("BUILD_ERROR", "TESTS_NOT_FOUND", "TIMEOUT"):
        return "BUILD_ERROR"
    return "FAILED"


# =============================================================================
# Helper: flakyrepro directory
# =============================================================================

def _flakyrepro_dir(repo_path, package, repo_slug):
    root = test_source_root_for(repo_slug)
    if package not in (".", ""):
        d = os.path.join(repo_path, package, root, "flakyrepro")
    else:
        d = os.path.join(repo_path, root, "flakyrepro")
    os.makedirs(d, exist_ok=True)
    return d


# =============================================================================
# Strategy implementations (adapted for patch evaluation)
# =============================================================================

def _run_programmatic_same_class(target, log_path, env):
    safe_key      = re.sub(r"[^a-zA-Z0-9]", "_", f"patch_{target['test_id']}")
    clone_dir     = target["clone_dir"]
    package       = target["package"]
    victim_class  = target["victim_class"]
    victim_meth   = target["victim_method"]
    polluter_meth = target["polluter_method"]
    repo_slug     = target["repo_slug"]
    offline       = offline_flag_for(repo_slug)
    mvn           = target.get("mvn_cmd", "mvn")
    pre_phase     = surefire_pre_phase_for(repo_slug)
    timeout       = surefire_timeout_for(repo_slug)

    fr_dir    = _flakyrepro_dir(clone_dir, package, repo_slug)
    java_path = os.path.join(fr_dir, f"ProgrammaticReproducer_{safe_key}.java")

    src = _SAME_CLASS_TEMPLATE.format(
        SAFE_KEY=safe_key,
        VICTIM_CLASS=victim_class,
        POLLUTER_METHOD=polluter_meth,
        VICTIM_METHOD=victim_meth,
    )
    with open(java_path, "w", encoding="utf-8") as f:
        f.write(src)

    pl = f"-pl '{package}'" if package not in (".", "") else ""
    test_class = f"flakyrepro.ProgrammaticReproducer_{safe_key}"

    # Compile reproducer
    compile_log = log_path + ".compile"
    compile_cmd = f"{mvn} {pl} test-compile -B {offline} {SKIP_FLAGS}"
    rc_c, out_c, _ = run_cmd(compile_cmd, clone_dir, PHASE_B_COMPILE_TIMEOUT, compile_log, env)
    if rc_c != 0:
        _cleanup(java_path)
        _cleanup(compile_log)
        return rc_c, out_c

    # Run reproducer
    surefire_goals = f"{pre_phase} surefire:test" if pre_phase else "surefire:test"
    run_cmd_str = (f"{mvn} {pl} {surefire_goals} -B {offline} "
                   f"-Dtest=\"{test_class}\" "
                   f"-DforkCount=1 -DreuseForks=true "
                   f"-Dsurefire.failIfNoSpecifiedTests=false "
                   f"{SKIP_FLAGS}")
    rc, output, elapsed = run_cmd(run_cmd_str, clone_dir, timeout, log_path, env)

    _cleanup(compile_log)
    _cleanup(java_path)
    return rc, output


def _run_programmatic_diff_class(target, log_path, env):
    safe_key       = re.sub(r"[^a-zA-Z0-9]", "_", f"patch_{target['test_id']}")
    clone_dir      = target["clone_dir"]
    package        = target["package"]
    victim_class   = target["victim_class"]
    victim_meth    = target["victim_method"]
    polluter_class = target["polluter_class"]
    polluter_meth  = target["polluter_method"]
    repo_slug      = target["repo_slug"]
    offline        = offline_flag_for(repo_slug)
    mvn            = target.get("mvn_cmd", "mvn")
    pre_phase      = surefire_pre_phase_for(repo_slug)
    timeout        = surefire_timeout_for(repo_slug)

    fr_dir    = _flakyrepro_dir(clone_dir, package, repo_slug)
    java_path = os.path.join(fr_dir, f"DiffClassReproducer_{safe_key}.java")

    src = _DIFF_CLASS_TEMPLATE.format(
        SAFE_KEY=safe_key,
        POLLUTER_CLASS=polluter_class,
        POLLUTER_METHOD=polluter_meth,
        VICTIM_CLASS=victim_class,
        VICTIM_METHOD=victim_meth,
    )
    with open(java_path, "w", encoding="utf-8") as f:
        f.write(src)

    pl = f"-pl '{package}'" if package not in (".", "") else ""
    test_class = f"flakyrepro.DiffClassReproducer_{safe_key}"

    compile_log = log_path + ".compile"
    compile_cmd = f"{mvn} {pl} test-compile -B {offline} {SKIP_FLAGS}"
    rc_c, out_c, _ = run_cmd(compile_cmd, clone_dir, PHASE_B_COMPILE_TIMEOUT, compile_log, env)
    if rc_c != 0:
        _cleanup(java_path)
        _cleanup(compile_log)
        return rc_c, out_c

    surefire_goals = f"{pre_phase} surefire:test" if pre_phase else "surefire:test"
    run_cmd_str = (f"{mvn} {pl} {surefire_goals} -B {offline} "
                   f"-Dtest=\"{test_class}\" "
                   f"-DforkCount=1 -DreuseForks=true "
                   f"-Dsurefire.failIfNoSpecifiedTests=false "
                   f"{SKIP_FLAGS}")
    rc, output, elapsed = run_cmd(run_cmd_str, clone_dir, timeout, log_path, env)

    _cleanup(compile_log)
    _cleanup(java_path)
    return rc, output


def _run_plus_syntax(target, log_path, env):
    clone_dir     = target["clone_dir"]
    package       = target["package"]
    victim_cls    = target["victim_class"].split(".")[-1]
    victim_meth   = target["victim_method"]
    polluter_cls  = target["polluter_class"].split(".")[-1]
    polluter_meth = target["polluter_method"]
    repo_slug     = target["repo_slug"]
    offline       = offline_flag_for(repo_slug)
    mvn           = target.get("mvn_cmd", "mvn")
    timeout       = surefire_timeout_for(repo_slug)

    pl = f"-pl '{package}'" if package not in (".", "") else ""
    test_spec = f"{polluter_cls}#{polluter_meth},{victim_cls}#{victim_meth}"
    cmd = (f"{mvn} surefire:test {pl} -B {offline} "
           f"-Dtest=\"{test_spec}\" "
           f"-DforkCount=1 -DreuseForks=true "
           f"-Dsurefire.runOrder=reversealphabetical "
           f"-Dsurefire.failIfNoSpecifiedTests=false "
           f"{SKIP_FLAGS}")

    rc, output, elapsed = run_cmd(cmd, clone_dir, timeout, log_path, env)
    return rc, output


def _run_custom_undertow(target, log_path, env):
    safe_key      = re.sub(r"[^a-zA-Z0-9]", "_", f"patch_{target['test_id']}")
    clone_dir     = target["clone_dir"]
    package       = target["package"]
    victim_class  = target["victim_class"]
    victim_meth   = target["victim_method"]
    polluter_meth = target["polluter_method"]
    mvn           = target.get("mvn_cmd", "mvn")
    timeout       = surefire_timeout_for(target["repo_slug"])

    fr_dir = os.path.join(clone_dir, package, "src", "test", "java", "flakyrepro")
    os.makedirs(fr_dir, exist_ok=True)

    runner_name = f"UndertowRunner_{safe_key}"
    suite_name  = f"UndertowSuite_{safe_key}"
    runner_path = os.path.join(fr_dir, f"{runner_name}.java")
    suite_path  = os.path.join(fr_dir, f"{suite_name}.java")

    with open(runner_path, "w", encoding="utf-8") as f:
        f.write(_UNDERTOW_RUNNER_TEMPLATE.format(
            SAFE_KEY=safe_key,
            POLLUTER_METHOD=polluter_meth,
            VICTIM_METHOD=victim_meth,
        ))
    with open(suite_path, "w", encoding="utf-8") as f:
        f.write(_UNDERTOW_SUITE_TEMPLATE.format(
            SAFE_KEY=safe_key,
            VICTIM_CLASS=victim_class,
        ))

    # Compile
    compile_flags = (f"-Dmaven.resources.skip=true "
                     f"-Dmaven.compiler.compilerArgument=-proc:none "
                     f"{SKIP_FLAGS}")
    compile_cmd = f"{mvn} -pl '{package}' -B -q {compile_flags} test-compile"
    compile_log = log_path + ".compile"
    rc_c, out_c, _ = run_cmd(compile_cmd, clone_dir, timeout, compile_log, env)

    # Run
    test_spec = f"flakyrepro.{suite_name}#{polluter_meth}+{victim_meth}"
    run_surefire = (f"{mvn} surefire:test -pl '{package}' -B "
                    f"-Dsurefire.runOrder=alphabetical "
                    f"-Dtest=\"{test_spec}\" "
                    f"-DforkCount=1 -DreuseForks=true "
                    f"-Dsurefire.failIfNoSpecifiedTests=false "
                    f"-Dsurefire.useFile=false "
                    f"{SKIP_FLAGS}")
    rc, output, elapsed = run_cmd(run_surefire, clone_dir, timeout, log_path, env)

    _cleanup(compile_log)
    _cleanup(runner_path)
    _cleanup(suite_path)
    return rc, output


# =============================================================================
# Unified OD test runner
# =============================================================================

def run_od_test(target, log_path, env=None):
    """Run the OD reproducer using the correct strategy for this target.

    The patched test file must already be written to disk before calling this.
    This function generates the reproducer, compiles, runs, and cleans up.

    Returns (test_status, test_output, test_elapsed).
      test_status: "PASSED" | "FAILED" | "BUILD_ERROR" | "TESTS_NOT_FOUND"
    """
    if env is None:
        env = make_env(target.get("required_java_version", 8))

    repo_slug  = target["repo_slug"]
    same_class = target["same_class"]
    strategy   = strategy_for(repo_slug, same_class)

    t0 = time.time()

    if strategy == "programmatic_same_class":
        _rc, output = _run_programmatic_same_class(target, log_path, env)
    elif strategy == "programmatic_diff_class":
        _rc, output = _run_programmatic_diff_class(target, log_path, env)
    elif strategy == "plus_syntax":
        _rc, output = _run_plus_syntax(target, log_path, env)
    elif strategy == "custom_undertow":
        _rc, output = _run_custom_undertow(target, log_path, env)
    else:
        output = f"Unknown strategy: {strategy}\n"
        elapsed = round(time.time() - t0, 2)
        return "BUILD_ERROR", output, elapsed

    elapsed = round(time.time() - t0, 2)
    repro_status = parse_repro_result(output, target["victim_method"], target["polluter_method"])
    test_status  = interpret_for_patch(repro_status)

    return test_status, output, elapsed


# =============================================================================
# Isolation runners
# =============================================================================

def run_victim_alone(target, log_path, env=None):
    """Run the victim test in isolation. Returns (status, output, elapsed)."""
    if env is None:
        env = make_env(target.get("required_java_version", 8))

    clone_dir    = target["clone_dir"]
    package      = target["package"]
    victim_class = target["victim_class"].split(".")[-1]
    victim_meth  = target["victim_method"]
    repo_slug    = target["repo_slug"]
    offline      = offline_flag_for(repo_slug)
    timeout      = surefire_timeout_for(repo_slug)
    mvn          = target.get("mvn_cmd", "mvn")

    pl  = f"-pl '{package}'" if package not in (".", "") else ""
    cmd = (f"{mvn} surefire:test {pl} -B {offline} "
           f"-Dtest=\"{victim_class}#{victim_meth}\" "
           f"-DforkCount=1 -DreuseForks=true "
           f"-Dsurefire.failIfNoSpecifiedTests=false "
           f"{SKIP_FLAGS}")

    rc, output, elapsed = run_cmd(cmd, clone_dir, timeout, log_path, env)
    status = parse_single_test_result(output, victim_meth)
    return status, output, elapsed


def run_polluter_alone(target, log_path, env=None):
    """Run the polluter test in isolation. Returns (status, output, elapsed)."""
    if env is None:
        env = make_env(target.get("required_java_version", 8))

    clone_dir      = target["clone_dir"]
    package        = target["package"]
    polluter_class = target["polluter_class"].split(".")[-1]
    polluter_meth  = target["polluter_method"]
    repo_slug      = target["repo_slug"]
    offline        = offline_flag_for(repo_slug)
    timeout        = surefire_timeout_for(repo_slug)
    mvn            = target.get("mvn_cmd", "mvn")

    # For same-class undertow, polluter is in the victim class
    if target["same_class"] and strategy_for(repo_slug, True) == "custom_undertow":
        test_class = target["victim_class"].split(".")[-1]
    else:
        test_class = polluter_class

    pl  = f"-pl '{package}'" if package not in (".", "") else ""
    cmd = (f"{mvn} surefire:test {pl} -B {offline} "
           f"-Dtest=\"{test_class}#{polluter_meth}\" "
           f"-DforkCount=1 -DreuseForks=true "
           f"-Dsurefire.failIfNoSpecifiedTests=false "
           f"{SKIP_FLAGS}")

    rc, output, elapsed = run_cmd(cmd, clone_dir, timeout, log_path, env)
    status = parse_single_test_result(output, polluter_meth)
    return status, output, elapsed


# =============================================================================
# API key check
# =============================================================================

def ensure_openai_api_key():
    """Check for OPENAI_API_KEY. If missing, prompt the user and set it."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    # Try loading from ~/.zshrc
    try:
        import subprocess as sp
        result = sp.run(
            ["bash", "-c", "grep -m1 'export OPENAI_API_KEY=' ~/.zshrc | sed 's/.*OPENAI_API_KEY=\"\\(.*\\)\"/\\1/'"],
            capture_output=True, text=True, timeout=5
        )
        key = result.stdout.strip().strip('"').strip("'")
        if key:
            os.environ["OPENAI_API_KEY"] = key
            return key
    except Exception:
        pass
    key = input("Enter your OpenAI API key: ").strip()
    if key:
        os.environ["OPENAI_API_KEY"] = key
        return key
    raise RuntimeError("OPENAI_API_KEY is required")
