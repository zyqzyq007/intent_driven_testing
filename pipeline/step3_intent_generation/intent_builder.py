"""
Intent Skeleton Builder
========================
Rule-driven generation of up to three IntentSkeletons per focal method.

No LLM is involved here — this module translates the BehavioralSemanticSlice
into structured GWT (Given–When–Then) skeletons using deterministic rules.
The skeletons are designed to be *complete enough to drive test generation*
later (Step 4), even without LLM enrichment.

Three intent dimensions
------------------------
FUNCTIONAL
    Normal execution path.
    Given:  required lifecycle states + data preconditions.
    When:   call the focal method with valid representative parameters.
    Then:   assert return value / data writes / downstream propagation.

BOUNDARY_EXCEPTION
    Null / empty / boundary / illegal-state inputs.
    Given:  object in an illegal state OR null/boundary parameter.
    When:   call with the edge-case input.
    Then:   expect exception / null return / graceful degradation.

INTERACTION_DEPENDENCY
    Generated only when the slice reveals state transitions or data propagation
    to other methods.
    Given:  initial state from prerequisite_states.
    When:   call the focal method.
    Then:   assert state changed to post_state AND downstream methods received data.
"""

from __future__ import annotations

import re
from typing import List, Optional

from .models import (
    BehavioralSemanticSlice,
    GivenContext,
    IntentSkeleton,
    IntentType,
    ThenEffect,
    WhenTrigger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_class(full_id: str) -> str:
    """
    Extracts the simple class name from an ESG node id.
    e.g.  "<spark.Service: void init()>"  →  "Service"
         "spark.Service.initialized_TRUE" →  "Service"
    """
    # Try JVM signature pattern
    m = re.search(r"<([^:>]+):", full_id)
    if m:
        fqn = m.group(1)          # e.g. "spark.Service"
        return fqn.split(".")[-1] # "Service"
    # Try state node pattern: "spark.Service.initialized_TRUE"
    parts = full_id.split(".")
    if len(parts) >= 2:
        return parts[-2]          # class is second-to-last segment
    return full_id


def _clean_data_label(label: str) -> str:
    """Returns the human-readable field name from a DATA node label."""
    return label.strip()


def _state_to_english(state_label: str) -> str:
    """
    Converts an ESG state label into a natural-language phrase.
    e.g. "initialized=FALSE"  → "Service is NOT initialized"
         "initialized=TRUE"   → "Service is initialized"
         "trustForwardHeaders=TRUE" → "trustForwardHeaders is enabled"
    """
    if "=" not in state_label:
        return state_label
    field, value = state_label.split("=", 1)
    field = re.sub(r"([A-Z])", r" \1", field).strip().lower()
    if value == "TRUE":
        return f"{field} is enabled / set to true"
    elif value == "FALSE":
        return f"{field} is NOT set (false)"
    else:
        return f"{field} == {value}"


def _guard_to_english(guard_label: str, state_label: str) -> str:
    """Converts a guard edge label + state into a precondition sentence."""
    state_en = _state_to_english(state_label)
    if "FALSE" in guard_label or "ne" in guard_label:
        return f"Requires: {state_en}"
    return f"Requires: {state_en}"


def _preceding_to_english(method_label: str, context: str) -> str:
    return f"{method_label}() must be called first (context: {context})"


def _data_to_english(data_label: str, access: str) -> str:
    if access in ("writes", "allocates"):
        return f"'{data_label}' is written / allocated"
    elif access == "read_and_passed_to":
        return f"'{data_label}' is read and passed downstream"
    elif access == "returned_by":
        return f"'{data_label}' is returned"
    return f"'{data_label}' ({access})"


def _call_position(slice_: BehavioralSemanticSlice) -> str:
    if slice_.preceding_calls:
        preds = [c.method_label for c in slice_.preceding_calls[:2]]
        return "called after " + ", ".join(f"{p}()" for p in preds)
    return "called directly"


def _method_call_signature(class_name: str, method_name: str) -> str:
    return f"{class_name}.{method_name}(...)"


def _slice_summary(slice_: BehavioralSemanticSlice) -> str:
    parts = []
    if slice_.prerequisite_states:
        parts.append(
            f"{len(slice_.prerequisite_states)} prerequisite state(s)"
        )
    if slice_.preceding_calls:
        parts.append(f"{len(slice_.preceding_calls)} preceding call(s)")
    if slice_.data_reads:
        parts.append(f"{len(slice_.data_reads)} data read(s)")
    if slice_.data_writes:
        parts.append(f"{len(slice_.data_writes)} data write(s)")
    if slice_.post_state_effects:
        parts.append(f"{len(slice_.post_state_effects)} state transition(s)")
    return "; ".join(parts) if parts else "no ESG context found"


# ---------------------------------------------------------------------------
# Per-type builders
# ---------------------------------------------------------------------------

def _build_functional(slice_: BehavioralSemanticSlice, focal_code: str) -> IntentSkeleton:
    """
    FUNCTIONAL intent: normal execution path with valid inputs.
    """
    given = GivenContext(
        lifecycle_states=[
            _guard_to_english(s.guard_label, s.state_label)
            for s in slice_.prerequisite_states
        ],
        setup_calls=[
            _preceding_to_english(c.method_label, c.context)
            for c in slice_.preceding_calls[:3]   # cap at 3 for readability
        ],
        data_preconditions=[
            f"'{d.data_label}' must be available (read dependency)"
            for d in slice_.data_reads[:3]
        ],
    )

    when = WhenTrigger(
        method_call   = _method_call_signature(slice_.focal_class, slice_.focal_method_label),
        call_position = _call_position(slice_),
        parameters    = ["provide valid representative inputs"],
    )

    then_state = [
        f"State transitions to: {_state_to_english(e.state_label)}"
        for e in slice_.post_state_effects
    ]
    then_data = [
        _data_to_english(d.data_label, d.access)
        for d in slice_.data_writes[:4]
    ]
    then_downstream = [
        f"Result propagated to {m}()"
        for m in slice_.downstream_calls[:3]
    ]
    if not (then_state or then_data or then_downstream):
        then_data = ["return value satisfies the expected contract"]

    return IntentSkeleton(
        focal_class  = slice_.focal_class,
        focal_method = slice_.focal_method_label,
        intent_type  = IntentType.FUNCTIONAL,
        given        = given,
        when         = when,
        then         = ThenEffect(
            state_changes      = then_state,
            data_effects       = then_data,
            downstream_effects = then_downstream,
        ),
        slice_summary = _slice_summary(slice_),
    )


def _build_boundary_exception(
    slice_: BehavioralSemanticSlice, focal_code: str
) -> IntentSkeleton:
    """
    BOUNDARY / EXCEPTION intent: null inputs or illegal lifecycle state.
    """
    # If there are prerequisite states, we can violate them for an illegal-state test.
    illegal_state_given: List[str] = []
    if slice_.prerequisite_states:
        for s in slice_.prerequisite_states:
            # Flip the guard: if it requires FALSE, we put it in TRUE (violate)
            if "FALSE" in s.state_label:
                illegal_state_given.append(
                    f"Precondition VIOLATED: {_state_to_english(s.state_label)} "
                    f"(object is already in TRUE state)"
                )
            else:
                illegal_state_given.append(
                    f"Precondition VIOLATED: {_state_to_english(s.state_label)} "
                    f"(object not yet initialised)"
                )
    else:
        illegal_state_given = ["no specific lifecycle preconditions — test null / boundary inputs"]

    given = GivenContext(
        lifecycle_states = illegal_state_given,
        setup_calls      = [],
        data_preconditions = [
            "pass null or empty value for each parameter",
            "pass boundary values (0, -1, MAX_INT, empty string)",
        ],
    )

    when = WhenTrigger(
        method_call   = _method_call_signature(slice_.focal_class, slice_.focal_method_label),
        call_position = "called with illegal / boundary input",
        parameters    = [
            "null for reference parameters",
            "0 or negative for numeric parameters",
            "empty string for String parameters",
        ],
    )

    # Check focal_code for null-check pattern
    has_null_check = "== null" in focal_code or "null ==" in focal_code
    has_throw = "throw" in focal_code or "Exception" in focal_code

    then_effects: List[str] = []
    if has_throw:
        then_effects.append("Expect a specific exception to be thrown (e.g. IllegalArgumentException / IllegalStateException)")
    if has_null_check:
        then_effects.append("Expect null return value when input is null")
    if not then_effects:
        then_effects = [
            "Expect NullPointerException, IllegalArgumentException, or graceful null return",
            "No partial state mutation should occur on failure",
        ]

    return IntentSkeleton(
        focal_class  = slice_.focal_class,
        focal_method = slice_.focal_method_label,
        intent_type  = IntentType.BOUNDARY_EXCEPTION,
        given        = given,
        when         = when,
        then         = ThenEffect(
            state_changes      = [],
            data_effects       = then_effects,
            downstream_effects = [],
        ),
        slice_summary = _slice_summary(slice_),
    )


def _build_interaction_dependency(
    slice_: BehavioralSemanticSlice, focal_code: str
) -> Optional[IntentSkeleton]:
    """
    INTERACTION / DEPENDENCY intent.
    Only generated when the slice has state transitions or data propagation
    to downstream methods — otherwise there is no interaction to verify.
    """
    if not slice_.post_state_effects and not slice_.downstream_calls:
        return None

    # Given: start from a specific known pre-state
    if slice_.prerequisite_states:
        given_states = [
            f"System starts in state: {_state_to_english(s.state_label)}"
            for s in slice_.prerequisite_states
        ]
    else:
        given_states = ["System in default initial state"]

    given = GivenContext(
        lifecycle_states   = given_states,
        setup_calls        = [
            _preceding_to_english(c.method_label, c.context)
            for c in slice_.preceding_calls[:2]
        ],
        data_preconditions = [
            f"'{d.data_label}' is properly initialised before call"
            for d in slice_.data_reads[:2]
        ],
    )

    when = WhenTrigger(
        method_call   = _method_call_signature(slice_.focal_class, slice_.focal_method_label),
        call_position = _call_position(slice_),
        parameters    = ["use the same parameters as in normal execution"],
    )

    then_state = [
        f"System state must change to: {_state_to_english(e.state_label)}"
        for e in slice_.post_state_effects
    ]
    then_downstream = [
        f"Downstream method {m}() must receive / observe the updated value"
        for m in slice_.downstream_calls[:4]
    ]
    then_data = [
        f"'{d.data_label}' must be updated in the system after the call"
        for d in slice_.data_writes[:3]
    ]

    return IntentSkeleton(
        focal_class  = slice_.focal_class,
        focal_method = slice_.focal_method_label,
        intent_type  = IntentType.INTERACTION_DEPENDENCY,
        given        = given,
        when         = when,
        then         = ThenEffect(
            state_changes      = then_state,
            data_effects       = then_data,
            downstream_effects = then_downstream,
        ),
        slice_summary = _slice_summary(slice_),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class IntentSkeletonBuilder:
    """
    Builds all applicable IntentSkeletons from a BehavioralSemanticSlice.

    Always produces FUNCTIONAL + BOUNDARY_EXCEPTION.
    Conditionally produces INTERACTION_DEPENDENCY when the slice has
    state transitions or downstream propagation.
    """

    def build(
        self,
        slice_: BehavioralSemanticSlice,
        focal_code: str,
    ) -> List[IntentSkeleton]:
        intents: List[IntentSkeleton] = []

        # 1. Functional — always present
        intents.append(_build_functional(slice_, focal_code))

        # 2. Boundary / Exception — always present
        intents.append(_build_boundary_exception(slice_, focal_code))

        # 3. Interaction / Dependency — only when ESG shows state/data effects
        interaction = _build_interaction_dependency(slice_, focal_code)
        if interaction is not None:
            intents.append(interaction)

        return intents
