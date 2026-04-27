#!/usr/bin/env python3
"""
config.py — Central configuration for the failure data collection pipeline.

=============================================================================
WHAT IS NOT GENERALIZABLE TO ARBITRARY NEW PAIRS
(must be reviewed/updated when adding new repos)
=============================================================================

1. JAVA_HOME PATHS
   Read from environment variables JAVA8_HOME, JAVA11_HOME, JAVA17_HOME.
   macOS paths are used as fallbacks. On a different machine these will be wrong.
   → Set the env vars in your shell profile before running.

2. UNDERTOW REPRODUCTION STRATEGY
   The 'custom_undertow' strategy is hard-wired to undertow's DefaultServer
   test infrastructure (package layout, setup() / after() method names,
   AnnotatedClientEndpoint.reset(), etc.). It will not work for any other repo.

3. SPRING-BOOT REPRODUCTION STRATEGY
   Spring Boot's strict Checkstyle rejects any generated Java file in the
   source tree. 'plus_syntax' bypasses this by running surefire:test directly
   (skipping compile). Works only because all spring-boot polluter class names
   sort alphabetically after victim class names (reversealphabetical ordering).
   Verify this assumption when adding new spring-boot pairs.

4. TIMEOUT OVERRIDES (TIMEOUT_OVERRIDES dict below)
   Per-repo timeout values were determined empirically. New repos may need
   different values.

5. OFFLINE_REPOS (set below)
   Repos that need Maven's --offline flag to avoid re-downloading artifacts.
   Add new repos here if they have network issues during test execution.

=============================================================================
"""

import os

# ── Java home resolution ───────────────────────────────────────────────────────

_JAVA_FALLBACKS = {
    8:  "/Library/Java/JavaVirtualMachines/adoptopenjdk-8.jdk/Contents/Home",
    11: "/opt/homebrew/Cellar/openjdk@11/11.0.30/libexec/openjdk.jdk/Contents/Home",
    17: "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    21: "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
}

_JAVA_ENV_VARS = {
    8:  "JAVA8_HOME",
    11: "JAVA11_HOME",
    17: "JAVA17_HOME",
    21: "JAVA21_HOME",
}


def _build_java_homes():
    homes = {}
    for ver, env_var in _JAVA_ENV_VARS.items():
        path = os.environ.get(env_var, _JAVA_FALLBACKS.get(ver, ""))
        if path and os.path.isfile(os.path.join(path, "bin", "javac")):
            homes[ver] = path
        elif path:
            # Path set but javac not found — warn but don't crash at import time
            pass
    return homes


JAVA_HOMES = _build_java_homes()


def java_home_for(required_version):
    """Return (java_home_path, actual_version, note) or (None, None, error_msg).

    Falls back to higher available versions if exact match isn't installed.
    """
    v = required_version or 8

    if v in JAVA_HOMES:
        return JAVA_HOMES[v], v, None

    # Fallback: nearest higher version
    for fallback in sorted(JAVA_HOMES.keys()):
        if fallback >= v:
            note = f"Java {v} not found; using Java {fallback} as fallback"
            return JAVA_HOMES[fallback], fallback, note

    return None, None, f"No suitable JDK found for Java {v}. Available: {sorted(JAVA_HOMES.keys())}"


def make_env(java_home):
    """Return a copy of os.environ with JAVA_HOME and PATH set for the given JDK."""
    env = os.environ.copy()
    env["JAVA_HOME"] = java_home
    env["PATH"] = os.path.join(java_home, "bin") + ":" + env.get("PATH", "")
    return env


# ── Maven skip flags ───────────────────────────────────────────────────────────
# Suppress non-test plugins that would otherwise block or slow the build.

SKIP_FLAGS = (
    "-Drat.skip=true "
    "-Dcheckstyle.skip=true "
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

# ── Timeouts (seconds) ─────────────────────────────────────────────────────────

DEFAULT_COMPILE_TIMEOUT        = 300
DEFAULT_SUREFIRE_TIMEOUT       = 360
# Compile timeout for Phase B programmatic reproducers.  Generating a new
# .java file in a large module triggers a full incremental recompile; 1200s
# accommodates modules with 100+ test classes (e.g. shardingsphere-elasticjob).
PHASE_B_COMPILE_TIMEOUT        = 1200

# Per-repo overrides keyed by repo_slug (last segment of GitHub URL).
# Add new entries here when a repo consistently hits the default timeout.
TIMEOUT_OVERRIDES = {
    "undertow":                 600,
    "spring-boot":              900,
    "flow":                     600,
    "openhtmltopdf":            600,
    "shardingsphere-elasticjob": 600,
    "http-request":             600,
}


def surefire_timeout_for(repo_slug):
    return TIMEOUT_OVERRIDES.get(repo_slug, DEFAULT_SUREFIRE_TIMEOUT)


# ── Reproduction strategy overrides ───────────────────────────────────────────
# Default strategy is selected automatically based on same_class flag.
# Override here for repos with special test infrastructure requirements.
#
# Available strategies:
#   "programmatic_same_class"  — reflection-based, polluter+victim on same instance
#   "programmatic_diff_class"  — reflection-based, separate class instances
#   "plus_syntax"              — surefire PLUS_SYNTAX + reversealphabetical
#   "custom_undertow"          — @RunWith(DefaultServer.class) with explicit calls

STRATEGY_OVERRIDES = {
    "undertow":    "custom_undertow",
    "spring-boot": "plus_syntax",
}


def strategy_for(repo_slug, same_class):
    """Return the reproduction strategy name for this pair."""
    if repo_slug in STRATEGY_OVERRIDES:
        return STRATEGY_OVERRIDES[repo_slug]
    return "programmatic_same_class" if same_class else "programmatic_diff_class"


# ── Offline repos ──────────────────────────────────────────────────────────────
# Repos where Maven should run with --offline during test execution to avoid
# re-downloading artifacts (these repos have all deps cached locally).
# NOT generalizable — add/remove as needed.

OFFLINE_REPOS = {
    "http-request",
    "openhtmltopdf",
    "shardingsphere-elasticjob",
    "cukes",        # cukes-*:0.0.33-SNAPSHOT not in any public repo
}


def offline_flag_for(repo_slug):
    return "--offline" if repo_slug in OFFLINE_REPOS else ""


# ── Test source directory overrides ───────────────────────────────────────────
# Repos whose test source root is NOT the Maven default (src/test/java).
# Value is the path *relative to the module root* (i.e. the package directory).
# NOT generalizable — add new entries when a repo uses a non-standard layout.

TEST_SOURCE_DIR_OVERRIDES = {
    # UGS: <sourceDirectory>src/</sourceDirectory>  <testSourceDirectory>test/</testSourceDirectory>
    # Placing files in src/test/java/ would land inside the main source tree.
    "Universal-G-Code-Sender": "test",
}


def test_source_root_for(repo_slug, default="src/test/java"):
    """Return the test source root relative to the module directory."""
    return TEST_SOURCE_DIR_OVERRIDES.get(repo_slug, default)


# ── Surefire pre-phase overrides ───────────────────────────────────────────────
# Repos whose surefire:test invocation must be prefixed with a lifecycle phase
# so that plugin goals bound to early phases (e.g. JaCoCo prepare-agent) run
# in the same Maven session and can resolve @{argLine} late-binding tokens.
# NOT generalizable — add new entries as needed.

SUREFIRE_PRE_PHASES = {
    # Sentinel pom uses @{argLine} in surefire config; JaCoCo prepare-agent must
    # run in the same Maven session to set the argLine property before surefire:test.
    "Sentinel": "initialize",
}


def surefire_pre_phase_for(repo_slug):
    """Return a lifecycle phase to prepend before surefire:test, or empty string."""
    return SUREFIRE_PRE_PHASES.get(repo_slug, "")
