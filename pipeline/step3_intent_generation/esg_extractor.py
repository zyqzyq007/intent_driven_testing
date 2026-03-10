"""
ESG Context Extractor
=====================
Loads the ESG graph and, for each focal method, extracts a
BehavioralSemanticSlice that encodes:

  Given layer  ─ prerequisite states (STATE → METHOD edges with guard labels)
                 preceding calls     (METHOD → METHOD TEMPORAL edges)
                 data reads          (DATA   → METHOD CAUSAL edges)

  Then  layer  ─ post-state effects  (METHOD → STATE edges with transitions_to)
                 data writes         (METHOD → DATA  CAUSAL edges with writes/allocates)
                 downstream calls    (DATA   → METHOD edges that propagate the output)

Matching strategy
-----------------
The ESG node ids for methods look like:
    "<spark.Service: void init()>"

The focal_method in pairs.json is just the simple name, e.g. "init".
The focal_file_path gives us the class, e.g. "...spark/Service.java".

We match by:
  1. Extract class name from file path  (e.g. "Service")
  2. Scan ESG nodes for METHOD nodes whose id contains  "spark.<class>: "
     and whose label == focal_method_name
  3. If still ambiguous, pick the one with the most connected edges.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    BehavioralSemanticSlice,
    DataDependency,
    ESGEdge,
    ESGNode,
    NodeType,
    PostStateEffect,
    PrecedingCall,
    PrerequisiteState,
)
from pipeline.utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Graph index
# ---------------------------------------------------------------------------

class ESGGraph:
    """
    In-memory index of the ESG with fast lookup by node id and edge direction.
    """

    def __init__(self, data: dict):
        self.nodes: Dict[str, ESGNode] = {}
        self.out_edges: Dict[str, List[ESGEdge]] = {}   # source → [edges]
        self.in_edges:  Dict[str, List[ESGEdge]] = {}   # target → [edges]

        for raw in data.get("nodes", []):
            n = ESGNode.from_dict(raw)
            self.nodes[n.id] = n
            self.out_edges.setdefault(n.id, [])
            self.in_edges.setdefault(n.id, [])

        for raw in data.get("edges", []):
            e = ESGEdge.from_dict(raw)
            self.out_edges.setdefault(e.source, []).append(e)
            self.in_edges.setdefault(e.target, []).append(e)

    # ------------------------------------------------------------------
    def find_method_node(
        self, class_name: str, method_name: str
    ) -> Optional[str]:
        """
        Returns the ESG node id for the best matching method.

        Matching priority:
          1. id contains the class name segment AND label == method_name
          2. label == method_name  (fallback, picks most-connected)
        """
        # Normalize: ESG uses fully-qualified class names like "spark.Service"
        # but we only have the simple class name, e.g. "Service"
        class_segment = f".{class_name}:"   # e.g. ".Service:"

        candidates: List[Tuple[str, int]] = []
        for nid, node in self.nodes.items():
            if node.node_type != NodeType.METHOD:
                continue
            if node.label != method_name:
                continue
            degree = len(self.out_edges.get(nid, [])) + len(self.in_edges.get(nid, []))
            # Prefer nodes in the right class
            priority = (1 if class_segment in nid else 0, degree)
            candidates.append((nid, priority))

        if not candidates:
            return None

        # Sort by (class_match_score DESC, degree DESC)
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    @staticmethod
    def _class_from_file_path(file_path: str) -> str:
        """Extracts simple class name from an absolute file path."""
        stem = Path(file_path).stem   # e.g. "Service"
        return stem


# ---------------------------------------------------------------------------
# Slice extractor
# ---------------------------------------------------------------------------

# Labels that indicate the method guards a state requirement (precondition)
_GUARD_LABELS: Set[str] = {"guarded_by_TRUE", "guarded_by_FALSE",
                            "guarded_by_eq", "guarded_by_ne"}

# Labels that indicate data reading
_READ_LABELS: Set[str] = {"read_and_passed_to", "returned_by"}

# Labels that indicate data writing
_WRITE_LABELS: Set[str] = {"writes", "allocates"}


class ESGContextExtractor:
    """
    Extracts a BehavioralSemanticSlice for a single focal method.

    Usage:
        extractor = ESGContextExtractor(graph)
        slice_ = extractor.extract(focal_class, focal_method, focal_file_path)
    """

    def __init__(self, graph: ESGGraph):
        self.graph = graph

    def extract(
        self,
        focal_class: str,
        focal_method: str,
        focal_file_path: str,
    ) -> Optional[BehavioralSemanticSlice]:
        """
        Returns None if the method cannot be found in the ESG.
        """
        class_name = ESGGraph._class_from_file_path(focal_file_path)
        method_id  = self.graph.find_method_node(class_name, focal_method)

        if method_id is None:
            logger.debug(
                "Method not found in ESG: %s.%s  (file: %s)",
                class_name, focal_method, focal_file_path,
            )
            return None

        method_node = self.graph.nodes[method_id]

        slice_ = BehavioralSemanticSlice(
            focal_method_id    = method_id,
            focal_method_label = method_node.label,
            focal_class        = focal_class,
        )

        # ── Analyse IN-edges  (things that influence this method) ──────────
        for edge in self.graph.in_edges.get(method_id, []):
            src_node = self.graph.nodes.get(edge.source)
            if src_node is None:
                continue

            if src_node.node_type == NodeType.STATE:
                # Prerequisite state guard
                if edge.label in _GUARD_LABELS or "guarded_by" in edge.label:
                    slice_.prerequisite_states.append(
                        PrerequisiteState(
                            state_id    = src_node.id,
                            state_label = src_node.label,
                            guard_label = edge.label,
                        )
                    )

            elif src_node.node_type == NodeType.METHOD:
                # Temporal predecessor — extract the context method name
                if "follows_in_" in edge.label:
                    context = edge.label.replace("follows_in_", "")
                    slice_.preceding_calls.append(
                        PrecedingCall(
                            method_id    = src_node.id,
                            method_label = src_node.label,
                            context      = context,
                        )
                    )

            elif src_node.node_type == NodeType.DATA:
                # Data read (DATA → METHOD means data is consumed by method)
                if edge.label in _READ_LABELS or edge.label == "read_and_passed_to":
                    slice_.data_reads.append(
                        DataDependency(
                            data_id    = src_node.id,
                            data_label = src_node.label,
                            access     = edge.label,
                        )
                    )

        # ── Analyse OUT-edges (things this method affects) ─────────────────
        for edge in self.graph.out_edges.get(method_id, []):
            tgt_node = self.graph.nodes.get(edge.target)
            if tgt_node is None:
                continue

            if tgt_node.node_type == NodeType.STATE:
                if "transitions_to" in edge.label:
                    slice_.post_state_effects.append(
                        PostStateEffect(
                            state_id    = tgt_node.id,
                            state_label = tgt_node.label,
                            transition  = edge.label,
                        )
                    )

            elif tgt_node.node_type == NodeType.DATA:
                if edge.label in _WRITE_LABELS:
                    slice_.data_writes.append(
                        DataDependency(
                            data_id    = tgt_node.id,
                            data_label = tgt_node.label,
                            access     = edge.label,
                        )
                    )

            elif tgt_node.node_type == NodeType.METHOD:
                # Temporal successor or data-driven call
                slice_.downstream_calls.append(tgt_node.label)

        # De-duplicate downstream calls
        slice_.downstream_calls = list(dict.fromkeys(slice_.downstream_calls))

        return slice_


# ---------------------------------------------------------------------------
# Graph loader helper
# ---------------------------------------------------------------------------

def load_graph(esg_json_path: Path) -> Optional[ESGGraph]:
    """Loads esg_graph.json and returns an ESGGraph index."""
    if not esg_json_path.exists():
        logger.error("esg_graph.json not found: %s", esg_json_path)
        return None
    with open(esg_json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    graph = ESGGraph(data)
    logger.info(
        "ESGGraph indexed: %d nodes, %d edges",
        len(graph.nodes),
        sum(len(v) for v in graph.out_edges.values()),
    )
    return graph
