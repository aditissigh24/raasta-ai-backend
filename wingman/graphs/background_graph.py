"""
background_graph — LangGraph graph for post-turn processing.

Runs after the user has received the response (non-blocking).
Handles engagement evaluation and beat advancement.

Graph flow:
    START → should_eval? → eval_node (conditional) → beat_orchestrator → END
"""
from langgraph.graph import StateGraph, END

from wingman.state.wingman_state import BackgroundState
from wingman.nodes.engagement_eval_node import engagement_eval_node
from wingman.nodes.beat_orchestrator_node import beat_orchestrator_node
from config.settings import settings


def _should_run_eval(state: BackgroundState) -> str:
    """
    Run eval every EVAL_INTERVAL_TURNS turns.
    Uses totalTurns mod eval_interval to decide.
    """
    interval = settings.EVAL_INTERVAL_TURNS
    if interval > 0 and state["total_turns"] % interval == 0:
        return "eval_node"
    return "beat_orchestrator"


def build_background_graph() -> StateGraph:
    graph = StateGraph(BackgroundState)

    # Passthrough router — exists only to host the conditional edge
    graph.add_node("should_eval", lambda state: state)
    graph.add_node("eval_node", engagement_eval_node)
    graph.add_node("beat_orchestrator", beat_orchestrator_node)

    graph.set_entry_point("should_eval")

    graph.add_conditional_edges(
        "should_eval",
        _should_run_eval,
        {
            "eval_node":       "eval_node",
            "beat_orchestrator": "beat_orchestrator",
        },
    )

    graph.add_edge("eval_node", "beat_orchestrator")
    graph.add_edge("beat_orchestrator", END)

    return graph.compile()


# Module-level compiled graph — imported by pipeline.py
background_graph = build_background_graph()
