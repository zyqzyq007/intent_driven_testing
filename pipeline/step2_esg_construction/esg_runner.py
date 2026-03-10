"""
Step 2: ESG Construction
========================
Drives the Java/Soot-based Execution Semantic Graph builder and loads the
result back into Python for downstream consumption.

What this module does
---------------------
1. (Optional) Compile the ESG Java module with Maven  →  ``mvn compile``
2. Run the ESG analyser with Maven exec plugin         →  ``mvn exec:java``
   The Java process writes two files to *output_dir*:
       spark_esg.dot    – Graphviz DOT (human-readable)
       esg_graph.json   – Structured JSON (consumed by step 3+)
3. Load and validate ``esg_graph.json``, returning a plain Python dict.

All heavy-lifting lives in the Java side; this file is a thin subprocess
wrapper + loader.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from pipeline.utils import get_logger

logger = get_logger(__name__)

# Absolute path to the Maven project that contains the Soot-based analyser
_ESG_JAVA_PROJECT = Path(__file__).resolve().parents[2] / "esg_construction"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def compile_esg_module(java_project: Path = _ESG_JAVA_PROJECT) -> bool:
    """
    Runs ``mvn compile`` inside the ESG Java project.

    Returns True on success, False on failure.
    Skips compilation if the target/classes directory already exists and is
    newer than the pom.xml (i.e. already up-to-date).
    """
    classes_dir = java_project / "target" / "classes" / "com" / "esg"
    pom_path    = java_project / "pom.xml"

    if (
        classes_dir.exists()
        and pom_path.exists()
        and classes_dir.stat().st_mtime >= pom_path.stat().st_mtime
    ):
        logger.info("ESG module already compiled – skipping mvn compile.")
        return True

    logger.info("Compiling ESG Java module …")
    return _run_maven(["mvn", "compile", "-q"], cwd=java_project)


def run_esg_analysis(
    target_classes: Path,
    output_dir: Path,
    java_project: Path = _ESG_JAVA_PROJECT,
    reuse_existing: bool = True,
) -> bool:
    """
    Runs the Soot-based ESG analyser via ``mvn exec:java``.

    Parameters
    ----------
    target_classes:
        Path to the compiled .class files of the project under analysis.
    output_dir:
        Directory where ``spark_esg.dot`` and ``esg_graph.json`` will be written.
    java_project:
        Root of the Maven project that contains the analyser source.
    reuse_existing:
        If True and ``esg_graph.json`` already exists in *output_dir*, skip
        re-running the analysis.

    Returns True on success.
    """
    json_out = output_dir / "esg_graph.json"

    if reuse_existing and json_out.exists():
        logger.info("esg_graph.json already exists – reusing (pass reuse_existing=False to force rebuild).")
        return True

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Running ESG analysis …")
    logger.info("  target_classes : %s", target_classes)
    logger.info("  output_dir     : %s", output_dir)

    cmd = [
        "mvn", "exec:java",
        "-Dexec.mainClass=com.esg.Main",
        f"-Dexec.args={target_classes} {output_dir}",
        "-q",           # suppress Maven download noise; ESG prints its own logs
    ]
    return _run_maven(cmd, cwd=java_project)


# ---------------------------------------------------------------------------
# Graph loading
# ---------------------------------------------------------------------------

def load_esg_graph(output_dir: Path) -> Optional[Dict]:
    """
    Loads ``esg_graph.json`` from *output_dir* and returns a validated dict.

    Schema:
    {
        "nodes": [{"id", "label", "type", "allocation_site"?}, ...],
        "edges": [{"source", "target", "edge_type", "label"}, ...]
    }

    Returns None if the file is missing or malformed.
    """
    json_path = output_dir / "esg_graph.json"
    if not json_path.exists():
        logger.error("esg_graph.json not found at %s", json_path)
        return None

    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse esg_graph.json: %s", exc)
        return None

    nodes: List[Dict] = data.get("nodes", [])
    edges: List[Dict] = data.get("edges", [])
    logger.info(
        "ESG loaded: %d nodes  (%d METHOD, %d STATE, %d DATA),  %d edges  (%d TEMPORAL, %d STATE_TRANSITION, %d CAUSAL)",
        len(nodes),
        sum(1 for n in nodes if n.get("type") == "METHOD"),
        sum(1 for n in nodes if n.get("type") == "STATE"),
        sum(1 for n in nodes if n.get("type") == "DATA"),
        len(edges),
        sum(1 for e in edges if e.get("edge_type") == "TEMPORAL"),
        sum(1 for e in edges if e.get("edge_type") == "STATE_TRANSITION"),
        sum(1 for e in edges if e.get("edge_type") == "CAUSAL"),
    )
    return data


# ---------------------------------------------------------------------------
# Full pipeline runner
# ---------------------------------------------------------------------------

def run(
    project_root: Path,
    output_dir: Path,
    *,
    skip_compile: bool = False,
    reuse_existing: bool = True,
) -> Optional[Dict]:
    """
    Full Step-2 pipeline: compile → analyse → load.

    Parameters
    ----------
    project_root:
        Root of the Java project under test (e.g. spark-master/).
        Must have a ``target/classes`` directory already compiled.
    output_dir:
        Where to write ESG artefacts.
    skip_compile:
        Skip ``mvn compile`` for the ESG analyser module.
    reuse_existing:
        Skip analysis if ``esg_graph.json`` already exists.

    Returns the loaded graph dict, or None on failure.
    """
    logger.info("=== Step 2: ESG Construction ===")

    target_classes = project_root / "target" / "classes"
    if not target_classes.is_dir():
        logger.error(
            "target/classes not found at %s. "
            "Please run 'mvn compile -DskipTests' inside the project first.",
            target_classes,
        )
        return None

    # 1. Compile the ESG analyser (if needed)
    if not skip_compile:
        ok = compile_esg_module()
        if not ok:
            return None

    # 2. Run the analysis
    ok = run_esg_analysis(
        target_classes=target_classes,
        output_dir=output_dir,
        reuse_existing=reuse_existing,
    )
    if not ok:
        return None

    # 3. Load and return
    return load_esg_graph(output_dir)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_maven(cmd: List[str], cwd: Path) -> bool:
    """Executes a Maven command, streams stdout/stderr, returns success."""
    logger.info("$ %s  (cwd: %s)", " ".join(cmd), cwd)
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                logger.info("  [mvn] %s", line)
        proc.wait()
        elapsed = time.time() - t0
        if proc.returncode == 0:
            logger.info("Maven command succeeded in %.1fs", elapsed)
            return True
        else:
            logger.error("Maven command failed (exit %d) after %.1fs", proc.returncode, elapsed)
            return False
    except FileNotFoundError:
        logger.error("'mvn' not found. Please install Apache Maven and add it to PATH.")
        return False
    except Exception as exc:
        logger.error("Unexpected error running Maven: %s", exc)
        return False
