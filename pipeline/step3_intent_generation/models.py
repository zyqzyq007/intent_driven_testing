"""
Intent Data Models
==================
All dataclasses and enums used throughout Step 3.

Core concepts
-------------
BehavioralSemanticSlice
    A method-centric view of the ESG: what states must hold before the method
    runs, which methods precede it in the call order, what data it reads/writes,
    and what state changes it causes afterwards.

IntentSkeleton
    A structured Given–When–Then test intent derived from the slice, annotated
    with an IntentType so downstream steps can select the right test strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class IntentType(str, Enum):
    """
    Three orthogonal dimensions of test intent coverage.

    FUNCTIONAL
        Verifies the core return value / observable state after a normal call.
        → "given valid inputs, the method produces the correct output."

    BOUNDARY_EXCEPTION
        Exercises null inputs, boundary values, or illegal lifecycle states.
        → "given an empty/null/illegal input, the method throws / returns default."

    INTERACTION_DEPENDENCY
        Captures the method's effect on external state, lifecycle, or collaborators.
        → "given the system is in state S, calling the method transitions it to S'
           and propagates data D to downstream methods."
    """
    FUNCTIONAL            = "Functional"
    BOUNDARY_EXCEPTION    = "Boundary/Exception"
    INTERACTION_DEPENDENCY = "Interaction/Dependency"


class EdgeType(str, Enum):
    TEMPORAL         = "TEMPORAL"
    STATE_TRANSITION = "STATE_TRANSITION"
    CAUSAL           = "CAUSAL"


class NodeType(str, Enum):
    METHOD = "METHOD"
    STATE  = "STATE"
    DATA   = "DATA"


# ---------------------------------------------------------------------------
# ESG graph element wrappers (plain dataclasses for easy serialisation)
# ---------------------------------------------------------------------------

@dataclass
class ESGNode:
    id: str
    label: str
    node_type: NodeType
    allocation_site: Optional[str] = None

    @staticmethod
    def from_dict(d: dict) -> "ESGNode":
        return ESGNode(
            id=d["id"],
            label=d["label"],
            node_type=NodeType(d["type"]),
            allocation_site=d.get("allocation_site"),
        )


@dataclass
class ESGEdge:
    source: str
    target: str
    edge_type: EdgeType
    label: str

    @staticmethod
    def from_dict(d: dict) -> "ESGEdge":
        return ESGEdge(
            source=d["source"],
            target=d["target"],
            edge_type=EdgeType(d["edge_type"]),
            label=d["label"],
        )


# ---------------------------------------------------------------------------
# Behavioral Semantic Slice  (extracted from the ESG for one focal method)
# ---------------------------------------------------------------------------

@dataclass
class PrerequisiteState:
    """A state node that must hold before the focal method can legally execute."""
    state_id:    str          # ESG node id
    state_label: str          # human-readable  (e.g. "initialized=FALSE")
    guard_label: str          # edge label that connects state → method
                              # e.g. "guarded_by_FALSE" / "guarded_by_TRUE"


@dataclass
class PrecedingCall:
    """A method that must be called before the focal method (temporal ordering)."""
    method_id:    str
    method_label: str
    context:      str         # the 'follows_in_XXX' label extracted from the edge


@dataclass
class DataDependency:
    """A data node that the focal method reads or writes."""
    data_id:    str
    data_label: str
    access:     str           # "reads" | "writes" | "allocates" | "returned_by"
                              #  | "read_and_passed_to"


@dataclass
class PostStateEffect:
    """A state node the focal method transitions to after execution."""
    state_id:    str
    state_label: str
    transition:  str          # edge label, usually "transitions_to"


@dataclass
class BehavioralSemanticSlice:
    """
    Complete semantic context for a focal method, extracted from the ESG.

    This is the *intermediate representation* between the raw graph and the
    final structured intent.  It answers:
        - What lifecycle states are required?     (prerequisite_states)
        - What must have been called first?       (preceding_calls)
        - What data does the method consume?      (data_reads)
        - What data does the method produce?      (data_writes)
        - What state changes does it cause?       (post_state_effects)
        - What downstream methods consume its output? (downstream_calls)
    """
    focal_method_id:    str
    focal_method_label: str
    focal_class:        str

    # Given layer
    prerequisite_states: List[PrerequisiteState] = field(default_factory=list)
    preceding_calls:     List[PrecedingCall]     = field(default_factory=list)
    data_reads:          List[DataDependency]    = field(default_factory=list)

    # When layer
    # (The focal method itself is the When — no extra fields needed)

    # Then layer
    data_writes:         List[DataDependency]    = field(default_factory=list)
    post_state_effects:  List[PostStateEffect]   = field(default_factory=list)
    downstream_calls:    List[str]               = field(default_factory=list)

    def is_stateful(self) -> bool:
        return bool(self.prerequisite_states or self.post_state_effects)

    def has_data_flow(self) -> bool:
        return bool(self.data_reads or self.data_writes)

    def to_dict(self) -> dict:
        return {
            "focal_method_id":    self.focal_method_id,
            "focal_method_label": self.focal_method_label,
            "focal_class":        self.focal_class,
            "prerequisite_states": [vars(s) for s in self.prerequisite_states],
            "preceding_calls":    [vars(c) for c in self.preceding_calls],
            "data_reads":         [vars(d) for d in self.data_reads],
            "data_writes":        [vars(d) for d in self.data_writes],
            "post_state_effects": [vars(e) for e in self.post_state_effects],
            "downstream_calls":   self.downstream_calls,
        }


# ---------------------------------------------------------------------------
# Intent Skeleton  (the structured GWT output)
# ---------------------------------------------------------------------------

@dataclass
class GivenContext:
    """
    [Given] Precondition context.
    Captures lifecycle state requirements and necessary setup calls.
    """
    lifecycle_states:  List[str]   # natural-language descriptions of required states
    setup_calls:       List[str]   # methods that must be invoked first
    data_preconditions: List[str]  # data values / fields that must be initialised


@dataclass
class WhenTrigger:
    """
    [When] The focal method invocation and its position in the behaviour flow.
    """
    method_call:    str            # e.g. "service.port(8080)"
    call_position:  str            # e.g. "called after init()"
    parameters:     List[str]      # parameter descriptions


@dataclass
class ThenEffect:
    """
    [Then] Expected observable effects after the focal method executes.
    """
    state_changes:      List[str]  # state transitions caused
    data_effects:       List[str]  # data written / returned
    downstream_effects: List[str]  # downstream methods that receive the output


@dataclass
class IntentSkeleton:
    """
    A single structured test intent in GWT format.

    One focal method produces up to three IntentSkeletons (one per IntentType).
    focal_code is NOT stored here to avoid 3× duplication; it lives in
    IntentRecord.context_code.focal_code at the record level.
    """
    # Identity
    focal_class:  str
    focal_method: str
    intent_type:  IntentType

    # GWT structure
    given:  GivenContext
    when:   WhenTrigger
    then:   ThenEffect

    # Provenance
    slice_summary: str   # brief human-readable summary of the semantic slice used

    def to_dict(self) -> dict:
        return {
            "focal_class":    self.focal_class,
            "focal_method":   self.focal_method,
            "intent_type":    self.intent_type.value,
            "given": {
                "lifecycle_states":   self.given.lifecycle_states,
                "setup_calls":        self.given.setup_calls,
                "data_preconditions": self.given.data_preconditions,
            },
            "when": {
                "method_call":   self.when.method_call,
                "call_position": self.when.call_position,
                "parameters":    self.when.parameters,
            },
            "then": {
                "state_changes":      self.then.state_changes,
                "data_effects":       self.then.data_effects,
                "downstream_effects": self.then.downstream_effects,
            },
            "slice_summary": self.slice_summary,
        }

    def to_gwt_text(self) -> str:
        """Returns a human-readable GWT block for debugging / prompt construction."""
        lines = [
            f"[Intent Type] {self.intent_type.value}",
            f"[Focal]  {self.focal_class}.{self.focal_method}",
            "",
            "[Given]",
        ]
        for s in self.given.lifecycle_states:
            lines.append(f"  - State: {s}")
        for c in self.given.setup_calls:
            lines.append(f"  - Setup: {c}")
        for d in self.given.data_preconditions:
            lines.append(f"  - Data : {d}")
        if not (self.given.lifecycle_states or self.given.setup_calls or self.given.data_preconditions):
            lines.append("  - (no specific preconditions)")

        lines.append("")
        lines.append("[When]")
        lines.append(f"  - Call : {self.when.method_call}")
        lines.append(f"  - Position: {self.when.call_position}")
        for p in self.when.parameters:
            lines.append(f"  - Param: {p}")

        lines.append("")
        lines.append("[Then]")
        for s in self.then.state_changes:
            lines.append(f"  - State change : {s}")
        for d in self.then.data_effects:
            lines.append(f"  - Data effect  : {d}")
        for ds in self.then.downstream_effects:
            lines.append(f"  - Downstream   : {ds}")
        if not (self.then.state_changes or self.then.data_effects or self.then.downstream_effects):
            lines.append("  - (return value / no side effects)")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context Code  (all source-code snippets needed to generate tests)
# ---------------------------------------------------------------------------

@dataclass
class ContextCode:
    """
    A self-contained source-code context bundle for one focal method.

    Step 4 (test generation) can consume this directly without re-reading
    any source files.

    Fields
    ------
    focal_code
        Full source of the focal method itself.
    related_method_codes
        Dict of  method_label → source_code  for every method that appears
        in the slice's preceding_calls or downstream_calls.
        Populated on a best-effort basis; missing methods are excluded.
    field_definitions
        Dict of  field_name → declaration_snippet  for every DATA node that
        appears in data_reads or data_writes.
        e.g.  {"port": "private int port = DEFAULT_PORT;"}
    focal_class_imports
        The import block of the focal class file, so the test can replicate
        the same dependencies.
    """
    focal_code:           str
    related_method_codes: dict   # str → str
    field_definitions:    dict   # str → str
    focal_class_imports:  str    # raw import block (may be empty)

    def to_dict(self) -> dict:
        return {
            "focal_code":           self.focal_code,
            "related_method_codes": self.related_method_codes,
            "field_definitions":    self.field_definitions,
            "focal_class_imports":  self.focal_class_imports,
        }


# ---------------------------------------------------------------------------
# Top-level output record (one per focal-test pair)
# ---------------------------------------------------------------------------

@dataclass
class IntentRecord:
    """
    The complete intent generation output for one focal-test pair.

    Removed  : test_code  — the original test is legacy noise for Step 4.
    Added    : context_code — self-contained source bundle for test generation.
    """
    pair_id:     int
    test_class:  str   # kept for traceability (which test class to regenerate)
    test_method: str   # kept for traceability
    focal_class: str
    focal_method: str

    context_code: ContextCode              # all source code Step 4 needs
    slice:        BehavioralSemanticSlice  # structured ESG-derived context
    intents:      List[IntentSkeleton]     # GWT intent skeletons

    def to_dict(self) -> dict:
        return {
            "pair_id":      self.pair_id,
            "test_class":   self.test_class,
            "test_method":  self.test_method,
            "focal_class":  self.focal_class,
            "focal_method": self.focal_method,
            "context_code": self.context_code.to_dict(),
            "semantic_slice": self.slice.to_dict(),
            "intents": [i.to_dict() for i in self.intents],
        }
