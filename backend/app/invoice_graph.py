from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

GSTAnswer = Literal["ex_gst", "inc_gst"]
GST_BASES = {"ex_gst": "excluding_gst", "inc_gst": "including_gst"}


class CheckpointStateError(RuntimeError):
    pass


class InvoiceState(TypedDict, total=False):
    source_sha256: str
    target_amount: str | None
    submitted_gst_basis: str | None
    effective_gst_basis: str | None
    stage: Literal["ready_for_extraction"]


def target_evidence(state: InvoiceState) -> dict:
    return {"kind": "submitted_target", "amount": state["target_amount"], "gst_basis": "unknown"}


def route_inputs(state: InvoiceState) -> Literal["clarify_gst", "ready_for_extraction"]:
    if not state.get("source_sha256"):
        raise CheckpointStateError("Missing source")
    if state.get("target_amount") is not None and state.get("submitted_gst_basis") == "unknown":
        return "clarify_gst"
    return "ready_for_extraction"


def clarify_gst(state: InvoiceState) -> dict:
    answer = interrupt({"reason": "target_gst_basis_unknown", "evidence": target_evidence(state)})
    if not isinstance(answer, str) or answer not in GST_BASES:
        raise CheckpointStateError("Invalid GST answer")
    return {"effective_gst_basis": GST_BASES[answer]}


def ready_for_extraction(state: InvoiceState) -> dict:
    basis = state.get("effective_gst_basis", state.get("submitted_gst_basis"))
    if not state.get("source_sha256") or (
        state.get("target_amount") is not None and basis not in GST_BASES.values()
    ):
        raise CheckpointStateError("Extraction prerequisites are missing")
    return {"stage": "ready_for_extraction", "effective_gst_basis": basis}


def compile_invoice_graph(checkpointer):
    graph = StateGraph(InvoiceState)
    graph.add_node("clarify_gst", clarify_gst)
    graph.add_node("ready_for_extraction", ready_for_extraction)
    graph.add_conditional_edges(START, route_inputs)
    graph.add_edge("clarify_gst", "ready_for_extraction")
    graph.add_edge("ready_for_extraction", END)
    return graph.compile(checkpointer=checkpointer)
