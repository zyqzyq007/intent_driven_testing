#!/usr/bin/env python3
"""
Intent-Driven Testing Pipeline
================================
Orchestrates Step 1 (Input Transformation), Step 2 (ESG Construction),
and Step 3 (Intent Generation).

Usage
-----
# Run all three steps with default Spark project:
python run_pipeline.py

# Run all three steps on a custom project:
python run_pipeline.py --project /path/to/java-project

# Run only Step 1 (pair extraction):
python run_pipeline.py --steps 1

# Run only Step 2 (ESG construction):
python run_pipeline.py --steps 2

# Run only Step 3 (intent generation from existing artefacts):
python run_pipeline.py --steps 3

# Run Steps 1 & 2 only:
python run_pipeline.py --steps 12

# Run Steps 1, 2 & 3 (default):
python run_pipeline.py --steps 123

# Force re-run ESG even if esg_graph.json already exists:
python run_pipeline.py --steps 2 --no-reuse

# Skip Maven compile of the ESG module (already compiled):
python run_pipeline.py --steps 23 --skip-compile

Options
-------
--project PATH          Root of the Java project to analyse.
                        Default: data/raw/spark-master
--output-dir PATH       Root of the output directory.
                        Default: data/processed/<project-name>
--steps {1,2,3,12,23,123}  Which steps to run (default: 123 → all).
--no-reuse              Force re-run Step 2 even if artefacts exist.
--skip-compile          Skip 'mvn compile' for the ESG Java module.
--verbose               Enable DEBUG logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make sure we can import 'pipeline.*' regardless of working directory
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))

from pipeline.utils import get_logger
from pipeline.step1_input_transform import extractor as step1
from pipeline.step2_esg_construction import esg_runner as step2
from pipeline.step3_intent_generation import generator as step3

logger = get_logger("pipeline")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_PROJECT   = _REPO_ROOT / "data" / "raw"  / "spark-master"
DEFAULT_OUTPUT    = _REPO_ROOT / "data" / "processed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pairs_output(project_root: Path, output_root: Path) -> Path:
    return output_root / project_root.name / "pairs.json"

def _esg_output_dir(project_root: Path, output_root: Path) -> Path:
    return output_root / project_root.name


def _print_summary(label: str, data) -> None:
    sep = "─" * 60
    logger.info(sep)
    logger.info("  %s", label)
    logger.info(sep)
    if isinstance(data, list):
        logger.info("  Total items  : %d", len(data))
        # Show first 3 items as sample
        for item in data[:3]:
            logger.info(
                "  • %s.%s  →  %s.%s",
                item.get("test_class", "?"),
                item.get("test_method", "?"),
                item.get("focal_class", "?"),
                item.get("focal_method", "?"),
            )
        if len(data) > 3:
            logger.info("  … and %d more", len(data) - 3)
    elif isinstance(data, dict):
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        logger.info("  Nodes : %d  (METHOD=%d  STATE=%d  DATA=%d)",
                    len(nodes),
                    sum(1 for n in nodes if n.get("type") == "METHOD"),
                    sum(1 for n in nodes if n.get("type") == "STATE"),
                    sum(1 for n in nodes if n.get("type") == "DATA"))
        logger.info("  Edges : %d  (TEMPORAL=%d  STATE_TRANSITION=%d  CAUSAL=%d)",
                    len(edges),
                    sum(1 for e in edges if e.get("edge_type") == "TEMPORAL"),
                    sum(1 for e in edges if e.get("edge_type") == "STATE_TRANSITION"),
                    sum(1 for e in edges if e.get("edge_type") == "CAUSAL"))
    logger.info(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Intent-Driven Testing Pipeline – Steps 1, 2 & 3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project", type=Path, default=DEFAULT_PROJECT,
        help=f"Root of the Java project under analysis (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT,
        help=f"Root output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--steps", type=str, default="123",
        choices=["1", "2", "3", "12", "23", "123"],
        help="Which steps to run: '1'=extraction, '2'=ESG, '3'=intent generation; combine digits to run multiple (default: 123)",
    )
    parser.add_argument(
        "--no-reuse", action="store_true",
        help="Force re-run Step 2 ESG analysis even if esg_graph.json already exists",
    )
    parser.add_argument(
        "--skip-compile", action="store_true",
        help="Skip 'mvn compile' for the ESG Java module",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    project_root: Path = args.project.resolve()
    output_root:  Path = args.output_dir.resolve()

    if not project_root.is_dir():
        logger.error("Project path does not exist: %s", project_root)
        return 1

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Intent-Driven Testing Pipeline")
    logger.info("  Project   : %s", project_root)
    logger.info("  Output    : %s", output_root)
    logger.info("  Steps     : %s", args.steps)
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    pairs     = None
    esg_graph = None

    # ── Step 1: Input Transformation ────────────────────────────────────────
    if "1" in args.steps:
        pairs_out = _pairs_output(project_root, output_root)
        pairs = step1.run(project_root, pairs_out)
        if pairs is None:
            logger.error("Step 1 failed.")
            return 1
        _print_summary("Step 1 Result – Focal/Test Pairs", pairs)

    # ── Step 2: ESG Construction ─────────────────────────────────────────────
    if "2" in args.steps:
        esg_out_dir = _esg_output_dir(project_root, output_root)
        esg_graph = step2.run(
            project_root=project_root,
            output_dir=esg_out_dir,
            skip_compile=args.skip_compile,
            reuse_existing=not args.no_reuse,
        )
        if esg_graph is None:
            logger.error("Step 2 failed.")
            return 1
        _print_summary("Step 2 Result – Execution Semantic Graph", esg_graph)

    # ── Step 3: Intent Generation ────────────────────────────────────────────
    if "3" in args.steps:
        # Input artefacts expected under: <output_root>/<project_name>/
        pairs_out = _pairs_output(project_root, output_root)
        esg_graph_path = _esg_output_dir(project_root, output_root) / "esg_graph.json"
        intents_out = _esg_output_dir(project_root, output_root) / "intents.json"

        intents = step3.run(
            pairs_path = pairs_out,
            esg_json_path = esg_graph_path,
            output_path = intents_out,
        )
        if intents is None:
            logger.error("Step 3 failed.")
            return 1
        _print_summary("Step 3 Result – Generated Intents", intents)

    logger.info("Pipeline completed successfully ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
