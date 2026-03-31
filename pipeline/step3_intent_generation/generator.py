"""
Step 3: Intent Generation
=========================
Loads pairs.json + esg_graph.json, extracts a BehavioralSemanticSlice for
each focal method from the ESG, then produces structured GWT IntentSkeletons,
and saves the complete IntentRecord list to intents.json.

Output file: data/processed/<project>/intents.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .code_resolver import MethodCodeResolver
from .esg_extractor import ESGContextExtractor, load_graph
from .intent_builder import IntentSkeletonBuilder
from .models import BehavioralSemanticSlice, IntentRecord
from pipeline.utils import get_logger

logger = get_logger(__name__)


def run(
    pairs_path: Path,
    esg_json_path: Path,
    output_path: Path,
    project_root: Optional[Path] = None,
) -> Optional[List[Dict]]:
    """
    Full Step-3 pipeline: load → extract slice → build intents → save.

    Parameters
    ----------
    pairs_path    : data/processed/<project>/pairs.json
    esg_json_path : data/processed/<project>/esg_graph.json
    output_path   : data/processed/<project>/intents.json
    project_root  : root of the raw Java project (for source scanning).
                    If None, inferred as  esg_json_path.parent.parent.parent / 'raw' / <name>

    Returns the list of serialised IntentRecord dicts, or None on failure.
    """
    logger.info("=== Step 3: Intent Generation ===")
    logger.info("Pairs     : %s", pairs_path)
    logger.info("ESG graph : %s", esg_json_path)
    logger.info("Output    : %s", output_path)

    # ── 1. Load pairs ─────────────────────────────────────────────────────
    if not pairs_path.exists():
        logger.error("pairs.json not found: %s", pairs_path)
        return None
    with open(pairs_path, encoding="utf-8") as fh:
        pairs: List[Dict] = json.load(fh)
    logger.info("Loaded %d focal-test pairs", len(pairs))

    # ── 2. Load ESG graph ─────────────────────────────────────────────────
    graph = load_graph(esg_json_path)
    if graph is None:
        return None

    # ── 3. Infer project_root if not provided ─────────────────────────────
    if project_root is None:
        # esg_json_path is data/processed/<name>/esg_graph.json
        # project root is  data/raw/<name>
        proj_name    = esg_json_path.parent.name
        project_root = esg_json_path.parent.parent.parent / "raw" / proj_name
    logger.info("Project root: %s", project_root)

    # ── 4. Initialise extractor, builder, resolver ────────────────────────
    extractor = ESGContextExtractor(graph)
    builder   = IntentSkeletonBuilder()
    resolver  = MethodCodeResolver(project_root, pairs)

    records: List[Dict] = []
    n_found   = 0
    n_miss    = 0
    n_intents = 0

    # ── 5. Process each pair ──────────────────────────────────────────────
    for idx, pair in enumerate(pairs):
        focal_class     = pair.get("focal_class", "")
        focal_method    = pair.get("focal_method", "")
        focal_file_path = pair.get("focal_file_path", "")
        focal_code      = pair.get("focal_code", "")

        # Extract behavioral semantic slice from ESG
        slice_: Optional[BehavioralSemanticSlice] = extractor.extract(
            focal_class     = focal_class,
            focal_method    = focal_method,
            focal_file_path = focal_file_path,
        )

        if slice_ is None:
            from .models import BehavioralSemanticSlice as BSS
            slice_ = BSS(
                focal_method_id    = f"<UNKNOWN: {focal_class}.{focal_method}>",
                focal_method_label = focal_method,
                focal_class        = focal_class,
            )
            n_miss += 1
        else:
            n_found += 1

        # Resolve context code (related methods + field definitions + imports)
        context_code = resolver.resolve_context(
            focal_code       = focal_code,
            focal_file_path  = focal_file_path,
            preceding_calls  = slice_.preceding_calls,
            downstream_calls = slice_.downstream_calls,
            data_reads       = slice_.data_reads,
            data_writes      = slice_.data_writes,
            test_imports     = pair.get("test_imports", []),
        )

        # Build intent skeletons
        intents = builder.build(slice_, focal_code)
        n_intents += len(intents)

        record = IntentRecord(
            pair_id      = idx,
            test_class   = pair.get("test_class", ""),
            test_method  = pair.get("test_method", ""),
            focal_class  = focal_class,
            focal_method = focal_method,
            context_code = context_code,
            slice        = slice_,
            intents      = intents,
        )
        records.append(record.to_dict())

    logger.info(
        "ESG match: %d found / %d not found  (%.0f%%)",
        n_found, n_miss,
        100 * n_found / max(len(pairs), 1),
    )
    logger.info(
        "Generated %d intents across %d pairs  (avg %.1f per pair)",
        n_intents, len(pairs),
        n_intents / max(len(pairs), 1),
    )

    # ── 5. Save ───────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, ensure_ascii=False)
    logger.info("Intents saved → %s", output_path)

    return records
