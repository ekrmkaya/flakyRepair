#!/usr/bin/env python3
"""
Step 3: Two-Phase Flakiness Confirmation.

Phase A -- victim alone (must PASS):
  mvn surefire:test -pl '{package}' -B
    -Dtest="{VictimClass}#{victimMethod}"
    -DforkCount=1 -DreuseForks=true
    -Dsurefire.failIfNoSpecifiedTests=false
    {SKIP_FLAGS}

  If victim FAILS alone -> classify VICTIM_FAILS_ALONE, skip Phase B.

Phase B -- polluter then victim (strategy-specific):
  Strategy selected from config.strategy_for(repo_slug, same_class).

Step 3 status values:
  REPRODUCED                            -- Phase A PASSES_ALONE, Phase B victim fails
  NOT_REPRODUCED:VICTIM_PASSES_WITH_POLLUTER
  NOT_REPRODUCED:POLLUTER_ALSO_FAILED
  NOT_REPRODUCED:UNKNOWN
  VICTIM_FAILS_ALONE                    -- victim fails even without polluter
  TIMEOUT                               -- process killed after timeout
  BUILD_ERROR                           -- Maven output has no 'Tests run:' line
  TESTS_NOT_FOUND                       -- surefire found no matching test spec
  (inherits BUILD_FAILED / JAVA_NOT_FOUND / CLONE_FAILED etc. from prior steps)
"""

import json
import os
import re
import signal
import subprocess
import textwrap
import time

from config import (
    SKIP_FLAGS,
    PHASE_B_COMPILE_TIMEOUT,
    make_env,
    java_home_for,
    offline_flag_for,
    surefire_timeout_for,
    surefire_pre_phase_for,
    strategy_for,
    test_source_root_for,
)


# =============================================================================
# Command runner
# =============================================================================

def _kill_proc(proc):
    """Kill a process group (or just the process if the group is gone)."""
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


# Markers that indicate Maven/surefire finished its work, even if the
# JVM keeps running (e.g. lingering Jetty threads in http-request).
_DONE_MARKERS = ("Tests run:", "BUILD SUCCESS", "BUILD FAILURE",
                 "No tests were run", "No tests matching")
_POLL_SEC  = 2   # how often to check for markers
_GRACE_SEC = 5   # seconds after marker before killing


def _run_cmd(cmd, cwd, env, timeout, log_path):
    """Run a shell command, write stdout+stderr to log_path.

    Returns (exit_code, output_string).

    Output goes directly to a file (not piped through Python) so a
    hanging surefire forked JVM cannot block on pipe EOF.  The function
    polls the log for completion markers ("Tests run:", "BUILD SUCCESS",
    etc.).  Once tests finish but the process lingers (e.g. non-daemon
    threads), the entire process group is killed after a short grace
    period.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

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
        timed_out = False

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                _kill_proc(proc)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\nTIMEOUT after {timeout}s\n")
                break

            # Wait a short interval for the process to exit naturally
            try:
                proc.wait(timeout=min(_POLL_SEC, remaining))
                break  # process exited on its own
            except subprocess.TimeoutExpired:
                pass  # still running — check log markers

            # Check log for completion markers
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if any(m in content for m in _DONE_MARKERS):
                    if done_detected_at is None:
                        done_detected_at = time.time()
                    elif time.time() - done_detected_at >= _GRACE_SEC:
                        # Tests finished but process still alive — kill it
                        _kill_proc(proc)
                        break
            except (FileNotFoundError, IOError):
                pass

    except Exception as exc:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"ERROR launching command: {exc}\n")
        return 1, f"ERROR launching command: {exc}\n"

    with open(log_path, "r", encoding="utf-8") as f:
        output = f.read()
    rc = proc.returncode if proc.returncode is not None else 1
    return rc, output


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass


# =============================================================================
# Result parsers
# =============================================================================

def _parse_victim_alone_result(output, victim_method):
    """Classify the result of running the victim in isolation.

    Returns: "PASSES_ALONE" | "FAILS_ALONE" | "BUILD_ERROR" | "TESTS_NOT_FOUND"
    """
    if not re.search(r"Tests run:", output):
        return "BUILD_ERROR"
    if "No tests were run" in output:
        return "TESTS_NOT_FOUND"

    victim_failed = bool(
        re.search(rf"{re.escape(victim_method)}.*FAILURE", output) or
        re.search(rf"FAILURE.*{re.escape(victim_method)}", output) or
        re.search(r"Tests run:.*Failures: [1-9]", output) or
        re.search(r"Tests run:.*Errors: [1-9]", output)
    )
    return "FAILS_ALONE" if victim_failed else "PASSES_ALONE"


def _parse_repro_result(output, victim_method, polluter_method):
    """Classify the outcome of a polluter+victim run.

    Returns one of:
      REPRODUCED
      NOT_REPRODUCED:VICTIM_PASSES_WITH_POLLUTER
      NOT_REPRODUCED:POLLUTER_ALSO_FAILED
      NOT_REPRODUCED:UNKNOWN
      BUILD_ERROR
      TESTS_NOT_FOUND
      TIMEOUT
    """
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


# =============================================================================
# Java reproducer templates
# =============================================================================

_SAME_CLASS_TEMPLATE = textwrap.dedent("""\
package flakyrepro;

import org.junit.Test;
import java.lang.reflect.*;
import java.util.*;

/** Generated by step3_reproduce.py -- deleted after run. */
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

        // @BeforeClass
        for (Method m : getAnnotated(cls, org.junit.BeforeClass.class)) m.invoke(null);

        // --- POLLUTER ---
        for (Method m : getAnnotated(cls, org.junit.Before.class))  m.invoke(inst);
        try {{
            cls.getMethod("{POLLUTER_METHOD}").invoke(inst);
        }} catch (java.lang.reflect.InvocationTargetException __) {{
            // Swallow: polluter may carry @Test(expected=...) -- the pollution
            // side-effect has already occurred before the exception is thrown.
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

/** Generated by step3_reproduce.py -- deleted after run. */
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
        // -- POLLUTER CLASS ---------------------------------------------------
        Class<?> polCls  = Class.forName("{POLLUTER_CLASS}");
        Object   polInst = polCls.getDeclaredConstructor().newInstance();
        for (Method m : getAnnotated(polCls, org.junit.BeforeClass.class)) m.invoke(null);
        for (Method m : getAnnotated(polCls, org.junit.Before.class))      m.invoke(polInst);
        polCls.getMethod("{POLLUTER_METHOD}").invoke(polInst);
        for (Method m : getAnnotated(polCls, org.junit.After.class))       m.invoke(polInst);
        for (Method m : getAnnotated(polCls, org.junit.AfterClass.class))  m.invoke(null);

        // -- VICTIM CLASS -----------------------------------------------------
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

/** Generated by step3_reproduce.py -- deleted after run. */
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

/** Generated by step3_reproduce.py -- deleted after run. */
@RunWith(UndertowRunner_{SAFE_KEY}.class)
public class UndertowSuite_{SAFE_KEY} extends {VICTIM_CLASS} {{
}}
""")


# =============================================================================
# Strategy implementations
# =============================================================================

def _flakyrepro_dir(repo_path, package, repo_slug, subdir="src/test/java"):
    """Return the flakyrepro/ directory under the module's test source tree."""
    root = test_source_root_for(repo_slug, default=subdir)
    if package not in (".", ""):
        d = os.path.join(repo_path, package, root, "flakyrepro")
    else:
        d = os.path.join(repo_path, root, "flakyrepro")
    os.makedirs(d, exist_ok=True)
    return d


def _run_programmatic_same_class(pair, repo_path, logs_dir, env, timeout, log_path):
    safe_key      = pair["row_key"].replace("-", "_")
    package       = pair["package"]
    victim_class  = pair["victim_class"]
    victim_meth   = pair["victim_method"]
    polluter_meth = pair["polluter_method"]
    repo_slug     = pair["repo_slug"]
    offline       = offline_flag_for(repo_slug)
    mvn           = pair.get("mvn_cmd", "mvn")
    pre_phase     = surefire_pre_phase_for(repo_slug)

    fr_dir    = _flakyrepro_dir(repo_path, package, repo_slug)
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

    # Step 1: compile
    compile_cmd = f"{mvn} {pl} test-compile -B {offline} {SKIP_FLAGS}"
    compile_log = log_path + ".compile"
    rc_c, out_c = _run_cmd(compile_cmd, repo_path, env, PHASE_B_COMPILE_TIMEOUT, compile_log)
    if rc_c != 0:
        _cleanup(java_path)
        # Copy compile log to main log
        try:
            with open(compile_log, "r") as cf:
                content = cf.read()
            with open(log_path, "w") as lf:
                lf.write(content)
        except OSError:
            pass
        _cleanup(compile_log)
        return rc_c, out_c

    # Step 2: surefire:test
    surefire_goals = f"{pre_phase} surefire:test" if pre_phase else "surefire:test"
    run_cmd_str = (f"{mvn} {pl} {surefire_goals} -B {offline} "
                   f"-Dtest=\"{test_class}\" "
                   f"-DforkCount=1 -DreuseForks=true "
                   f"-Dsurefire.failIfNoSpecifiedTests=false "
                   f"{SKIP_FLAGS}")
    rc, output = _run_cmd(run_cmd_str, repo_path, env, timeout, log_path)

    # Append compile output for full log
    try:
        with open(compile_log, "r") as cf:
            compile_content = cf.read()
        with open(log_path, "a") as lf:
            lf.write(f"\n\n--- COMPILE STEP (prepended) ---\n{compile_content}")
    except OSError:
        pass
    _cleanup(compile_log)
    _cleanup(java_path)
    return rc, output


def _run_programmatic_diff_class(pair, repo_path, logs_dir, env, timeout, log_path):
    safe_key       = pair["row_key"].replace("-", "_")
    package        = pair["package"]
    victim_class   = pair["victim_class"]
    victim_meth    = pair["victim_method"]
    polluter_class = pair["polluter_class"]
    polluter_meth  = pair["polluter_method"]
    repo_slug      = pair["repo_slug"]
    offline        = offline_flag_for(repo_slug)
    mvn            = pair.get("mvn_cmd", "mvn")
    pre_phase      = surefire_pre_phase_for(repo_slug)

    fr_dir    = _flakyrepro_dir(repo_path, package, repo_slug)
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

    # Step 1: compile
    compile_cmd = f"{mvn} {pl} test-compile -B {offline} {SKIP_FLAGS}"
    compile_log = log_path + ".compile"
    rc_c, out_c = _run_cmd(compile_cmd, repo_path, env, PHASE_B_COMPILE_TIMEOUT, compile_log)
    if rc_c != 0:
        _cleanup(java_path)
        try:
            with open(compile_log, "r") as cf:
                content = cf.read()
            with open(log_path, "w") as lf:
                lf.write(content)
        except OSError:
            pass
        _cleanup(compile_log)
        return rc_c, out_c

    # Step 2: surefire:test
    surefire_goals = f"{pre_phase} surefire:test" if pre_phase else "surefire:test"
    run_cmd_str = (f"{mvn} {pl} {surefire_goals} -B {offline} "
                   f"-Dtest=\"{test_class}\" "
                   f"-DforkCount=1 -DreuseForks=true "
                   f"-Dsurefire.failIfNoSpecifiedTests=false "
                   f"{SKIP_FLAGS}")
    rc, output = _run_cmd(run_cmd_str, repo_path, env, timeout, log_path)

    try:
        with open(compile_log, "r") as cf:
            compile_content = cf.read()
        with open(log_path, "a") as lf:
            lf.write(f"\n\n--- COMPILE STEP (prepended) ---\n{compile_content}")
    except OSError:
        pass
    _cleanup(compile_log)
    _cleanup(java_path)
    return rc, output


def _run_plus_syntax(pair, repo_path, logs_dir, env, timeout, log_path):
    package       = pair["package"]
    victim_cls    = pair["victim_class"].split(".")[-1]
    victim_meth   = pair["victim_method"]
    polluter_cls  = pair["polluter_class"].split(".")[-1]
    polluter_meth = pair["polluter_method"]
    offline       = offline_flag_for(pair["repo_slug"])
    mvn           = pair.get("mvn_cmd", "mvn")

    pl = f"-pl '{package}'" if package not in (".", "") else ""
    test_spec = f"{polluter_cls}#{polluter_meth},{victim_cls}#{victim_meth}"
    cmd = (f"{mvn} surefire:test {pl} -B {offline} "
           f"-Dtest=\"{test_spec}\" "
           f"-DforkCount=1 -DreuseForks=true "
           f"-Dsurefire.runOrder=reversealphabetical "
           f"-Dsurefire.failIfNoSpecifiedTests=false "
           f"{SKIP_FLAGS}")

    return _run_cmd(cmd, repo_path, env, timeout, log_path)


def _run_undertow(pair, repo_path, logs_dir, env, timeout, log_path):
    safe_key      = pair["row_key"].replace("-", "_")
    package       = pair["package"]
    victim_class  = pair["victim_class"]
    victim_meth   = pair["victim_method"]
    polluter_meth = pair["polluter_method"]
    mvn           = pair.get("mvn_cmd", "mvn")

    fr_dir = os.path.join(repo_path, package, "src", "test", "java", "flakyrepro")
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

    # Step 1: compile
    compile_flags = (f"-Dmaven.resources.skip=true "
                     f"-Dmaven.compiler.compilerArgument=-proc:none "
                     f"{SKIP_FLAGS}")
    compile_cmd = f"{mvn} -pl '{package}' -B -q {compile_flags} test-compile"
    compile_log = log_path + ".compile"

    rc_c, out_c = _run_cmd(compile_cmd, repo_path, env, timeout, compile_log)
    # Retry once if file-write timeout
    if rc_c != 0 and "Operation timed out" in out_c:
        rc_c, out_c = _run_cmd(compile_cmd, repo_path, env, timeout, compile_log)

    # Step 2: surefire:test
    test_spec = f"flakyrepro.{suite_name}#{polluter_meth}+{victim_meth}"
    run_surefire = (f"{mvn} surefire:test -pl '{package}' -B "
                    f"-Dsurefire.runOrder=alphabetical "
                    f"-Dtest=\"{test_spec}\" "
                    f"-DforkCount=1 -DreuseForks=true "
                    f"-Dsurefire.failIfNoSpecifiedTests=false "
                    f"-Dsurefire.useFile=false "
                    f"{SKIP_FLAGS}")

    rc, output = _run_cmd(run_surefire, repo_path, env, timeout, log_path)

    # Merge compile log into main log
    compile_out = ""
    if os.path.isfile(compile_log):
        with open(compile_log) as f:
            compile_out = f.read()
        _cleanup(compile_log)
    try:
        with open(log_path, "r+") as f:
            body = f.read()
            f.seek(0)
            f.write(f"=== COMPILE (rc={rc_c}) ===\n{compile_out}\n=== SUREFIRE ===\n{body}")
    except OSError:
        pass

    _cleanup(runner_path)
    _cleanup(suite_path)
    return rc, output


# =============================================================================
# Helpers
# =============================================================================

def _write_manifest(pairs, manifest_path):
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pairs": pairs}, f, indent=2)
    os.replace(tmp, manifest_path)


def _short_class(fqn):
    """Return simple class name from fully-qualified class name."""
    return fqn.split(".")[-1]


# =============================================================================
# Phase A -- victim alone
# =============================================================================

def _run_victim_alone(pair, env, logs_dir):
    """Run the victim test in isolation. Returns (status, elapsed_sec, log_path)."""
    clone_dir    = pair["clone_dir"]
    package      = pair["package"]
    victim_class = _short_class(pair["victim_class"])
    victim_meth  = pair["victim_method"]
    repo_slug    = pair["repo_slug"]
    row_key      = pair["row_key"]
    offline      = offline_flag_for(repo_slug)
    timeout      = surefire_timeout_for(repo_slug)
    mvn          = pair.get("mvn_cmd", "mvn")

    pl  = f"-pl '{package}'" if package not in (".", "") else ""
    cmd = (f"{mvn} surefire:test {pl} -B {offline} "
           f"-Dtest=\"{victim_class}#{victim_meth}\" "
           f"-DforkCount=1 -DreuseForks=true "
           f"-Dsurefire.failIfNoSpecifiedTests=false "
           f"{SKIP_FLAGS}")

    log_path = os.path.join(logs_dir, f"{row_key}_phaseA.log")
    t0 = time.time()
    _rc, output = _run_cmd(cmd, clone_dir, env, timeout, log_path)
    elapsed = time.time() - t0

    status = _parse_victim_alone_result(output, victim_meth)
    return status, elapsed, log_path


# =============================================================================
# Phase B -- polluter + victim
# =============================================================================

def _run_phase_b(pair, env, logs_dir):
    """Run the selected reproduction strategy. Returns (status, elapsed_sec, log_path)."""
    clone_dir     = pair["clone_dir"]
    repo_slug     = pair["repo_slug"]
    row_key       = pair["row_key"]
    victim_meth   = pair["victim_method"]
    polluter_meth = pair["polluter_method"]
    same_class    = pair.get("same_class", False)
    timeout       = surefire_timeout_for(repo_slug)
    strategy      = strategy_for(repo_slug, same_class)

    log_path = os.path.join(logs_dir, f"{row_key}_phaseB.log")
    os.makedirs(logs_dir, exist_ok=True)

    t0 = time.time()

    if strategy == "programmatic_same_class":
        _rc, output = _run_programmatic_same_class(
            pair, clone_dir, logs_dir, env, timeout, log_path)

    elif strategy == "programmatic_diff_class":
        _rc, output = _run_programmatic_diff_class(
            pair, clone_dir, logs_dir, env, timeout, log_path)

    elif strategy == "plus_syntax":
        _rc, output = _run_plus_syntax(
            pair, clone_dir, logs_dir, env, timeout, log_path)

    elif strategy == "custom_undertow":
        _rc, output = _run_undertow(
            pair, clone_dir, logs_dir, env, timeout, log_path)

    else:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Unknown strategy: {strategy}\n")
        return "BUILD_ERROR", time.time() - t0, log_path

    elapsed = time.time() - t0
    status  = _parse_repro_result(output, victim_meth, polluter_meth)
    return status, elapsed, log_path


# =============================================================================
# Process one pair
# =============================================================================

def _process_pair(pair, logs_dir):
    """Run Phase A then (if appropriate) Phase B for one pair.
    Returns dict of new fields to merge into the pair.
    """
    repo_slug = pair["repo_slug"]
    req_java  = pair.get("required_java_version", 8)

    result = {
        "step3_status":        None,
        "strategy_used":       None,
        "victim_alone_sec":    0.0,
        "polluter_victim_sec": 0.0,
        "phase_a_log":         None,
        "phase_b_log":         None,
    }

    # Java environment
    java_home, actual_java, java_note = java_home_for(req_java)
    if java_home is None:
        result["step3_status"] = "JAVA_NOT_FOUND"
        print(f"  JAVA_NOT_FOUND: {java_note}")
        return result

    if java_note:
        print(f"  NOTE: {java_note}")

    env = make_env(java_home)

    # Strategy
    same_class = pair.get("same_class", False)
    strategy   = strategy_for(repo_slug, same_class)
    result["strategy_used"] = strategy

    # -- Phase A: victim alone -------------------------------------------------
    print(f"  Phase A (victim alone) ...", flush=True)
    phase_a_status, a_sec, a_log = _run_victim_alone(pair, env, logs_dir)
    result["victim_alone_sec"] = a_sec
    result["phase_a_log"]      = a_log

    print(f"  Phase A: {phase_a_status} ({a_sec:.1f}s)")

    if phase_a_status == "FAILS_ALONE":
        result["step3_status"] = "VICTIM_FAILS_ALONE"
        return result

    if phase_a_status in ("BUILD_ERROR", "TESTS_NOT_FOUND"):
        if strategy == "custom_undertow":
            print(f"  Phase A: cannot isolate victim with custom runner -- assuming PASSES_ALONE")
            phase_a_status = "PASSES_ALONE"
        else:
            result["step3_status"] = phase_a_status
            return result

    # phase_a_status == "PASSES_ALONE" -- proceed to Phase B
    # -- Phase B: polluter + victim --------------------------------------------
    print(f"  Phase B (strategy={strategy}) ...", flush=True)
    phase_b_status, b_sec, b_log = _run_phase_b(pair, env, logs_dir)
    result["polluter_victim_sec"] = b_sec
    result["phase_b_log"]         = b_log

    print(f"  Phase B: {phase_b_status} ({b_sec:.1f}s)")
    result["step3_status"] = phase_b_status
    return result


# =============================================================================
# Main step function
# =============================================================================

def run_step3(pairs, manifest_path, logs_dir, all_pairs=None):
    """Run two-phase reproduction for all BUILD_OK rows.
    Updates pairs in-place with step3_status and timing.
    Writes updated manifest after each row.

    all_pairs: if provided, used for manifest writes so the full dataset is
               preserved when pairs is a filtered subset (partial run).
    """
    manifest_pairs = all_pairs if all_pairs is not None else pairs
    os.makedirs(logs_dir, exist_ok=True)

    print("\n" + "=" * 70)
    print("STEP 3: Two-Phase Reproduction Confirmation")
    print("=" * 70)

    eligible = [p for p in pairs if p.get("step2_status") == "BUILD_OK"]
    print(f"{len(eligible)} BUILD_OK pairs to test\n")

    for idx, pair in enumerate(eligible, 1):
        row_key = pair["row_key"]
        repo    = pair["repo_slug"]
        vic     = pair["victim_method"]
        pol     = pair["polluter_method"]
        print(f"ROW {idx}/{len(eligible)}: {row_key}  repo={repo}")
        print(f"  victim={vic}  polluter={pol}")

        # Skip if already reproduced (resumable)
        existing = pair.get("step3_status")
        if existing is not None:
            print(f"  SKIP (already has step3_status={existing})")
            continue

        result = _process_pair(pair, logs_dir)
        pair.update(result)

        # Write manifest after each row so partial runs are resumable
        _write_manifest(manifest_pairs, manifest_path)

    counts = {}
    for p in pairs:
        s = (p.get("step3_status") or
             p.get("step2_status") or
             p.get("step1_status", "?"))
        counts[s] = counts.get(s, 0) + 1

    print(f"\nSTEP 3 SUMMARY: {counts}")
    return pairs


# =============================================================================
# Standalone runner
# =============================================================================

if __name__ == "__main__":
    import argparse

    HERE = os.path.dirname(os.path.abspath(__file__))
    _out = os.path.join(HERE, "output")

    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=_out)
    args = ap.parse_args()

    manifest_path = os.path.join(args.output_dir, "manifest.json")
    logs_dir      = os.path.join(args.output_dir, "logs")

    with open(manifest_path) as f:
        manifest = json.load(f)
    pairs = manifest["pairs"]

    run_step3(pairs, manifest_path, logs_dir)
