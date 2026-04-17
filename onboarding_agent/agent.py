"""
LangGraph onboarding agent.

Collects missing user profile data (name, age) and guides the user
through chapter → scenario selection before the roleplay session begins.

State is persisted in Redis between turns as a JSON blob so the graph
can be re-created fresh each turn with the prior context loaded in.

Graph topology (per-turn, runs to END each invocation):

    START → router
    router ──► collect_name      (steps: init, awaiting_name)
            ──► collect_age       (step:  awaiting_age)
            ──► present_chapters  (step:  awaiting_chapter)
            ──► present_scenarios (step:  awaiting_scenario)
    Each leaf node → END
"""
import json
import logging
from typing import Optional

from langgraph.graph import StateGraph, START, END

from onboarding_agent.state import OnboardingState
from onboarding_agent.nodes.collect_name import collect_name_node
from onboarding_agent.nodes.collect_age import collect_age_node
from onboarding_agent.nodes.present_chapters import present_chapters_node
from onboarding_agent.nodes.present_scenarios import present_scenarios_node
from config.redis_client import redis_client
logger = logging.getLogger(__name__)

# Redis key pattern for onboarding state
_STATE_KEY = "raasta:onboarding:state:{user_id}"
# 2-hour TTL — covers a full onboarding session with pauses
_STATE_TTL = 7200


# ---------------------------------------------------------------------------
# Redis persistence helpers
# ---------------------------------------------------------------------------

def _state_key(user_id: str) -> str:
    return _STATE_KEY.format(user_id=user_id)


async def load_onboarding_state(user_id: str) -> Optional[dict]:
    """Load persisted onboarding state from Redis. Returns None if not found."""
    raw = await redis_client.get(_state_key(user_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Could not decode onboarding state for {user_id}: {e}")
        return None


async def save_onboarding_state(user_id: str, state: dict) -> None:
    """Persist onboarding state to Redis with TTL."""
    persistable = {
        "current_step": state.get("current_step", "init"),
        "collected_name": state.get("collected_name"),
        "collected_age": state.get("collected_age"),
        "selected_chapter_id": state.get("selected_chapter_id"),
        "selected_chapter_name": state.get("selected_chapter_name"),
        "selected_scenario_id": state.get("selected_scenario_id"),
        "selected_character_id": state.get("selected_character_id"),
        "chapters": state.get("chapters", []),
        "scenarios": state.get("scenarios", []),
    }
    await redis_client.set(_state_key(user_id), json.dumps(persistable), ex=_STATE_TTL)


async def clear_onboarding_state(user_id: str) -> None:
    """Remove onboarding state from Redis (call after roleplay session ends if needed)."""
    await redis_client.delete(_state_key(user_id))


# ---------------------------------------------------------------------------
# Router: decides which node to run this turn
# ---------------------------------------------------------------------------

def _route_to_step(state: OnboardingState) -> str:
    step = state.get("current_step", "init")
    if step in ("init", "awaiting_name"):
        return "collect_name"
    if step == "awaiting_age":
        return "collect_age"
    if step == "awaiting_chapter":
        return "present_chapters"
    if step == "awaiting_scenario":
        return "present_scenarios"
    # "complete" or unknown — shouldn't be invoked again, but handle safely
    return END


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def _create_onboarding_graph():
    """Build and compile the onboarding LangGraph."""
    graph = StateGraph(OnboardingState)

    graph.add_node("collect_name", collect_name_node)
    graph.add_node("collect_age", collect_age_node)
    graph.add_node("present_chapters", present_chapters_node)
    graph.add_node("present_scenarios", present_scenarios_node)

    graph.add_conditional_edges(
        START,
        _route_to_step,
        {
            "collect_name": "collect_name",
            "collect_age": "collect_age",
            "present_chapters": "present_chapters",
            "present_scenarios": "present_scenarios",
            END: END,
        },
    )

    graph.add_edge("collect_name", END)
    graph.add_edge("collect_age", END)
    graph.add_edge("present_chapters", END)
    graph.add_edge("present_scenarios", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_onboarding_agent(
    user_id: str,
    user_config: dict,
    user_message: Optional[str] = None,
) -> dict:
    """
    Run one turn of the onboarding agent.

    Loads persisted state from Redis, appends the new user message,
    runs the LangGraph, saves updated state back to Redis.

    Args:
        user_id:      User's UID.
        user_config:  User profile dict from backend (name, age, etc.).
        user_message: Text the user just sent (None on first contact).

    Returns:
        dict with keys:
            reply_text         – what to send to the user
            options_to_send    – list of chip dicts, or None
            onboarding_complete – True when a scenario has been selected
            selected_scenario_id  – set when onboarding_complete=True
            selected_character_id – set when onboarding_complete=True
    """
    # Load prior state or start fresh
    saved = await load_onboarding_state(user_id)

    if saved:
        current_step = saved.get("current_step", "init")
        # If already complete, don't re-run — caller should not have called us
        if current_step == "complete":
            logger.warning(
                f"run_onboarding_agent called for {user_id} but state is already complete"
            )
            return {
                "reply_text": "",
                "options_to_send": None,
                "onboarding_complete": True,
                "selected_scenario_id": saved.get("selected_scenario_id"),
                "selected_character_id": saved.get("selected_character_id"),
            }
    else:
        # Determine initial step based on what we already know from user_config
        current_step = _infer_initial_step(user_config)
        saved = {}

    # Build initial state for this turn
    initial_state: OnboardingState = {
        "user_id": user_id,
        "user_config": user_config,
        "current_step": current_step,
        "latest_user_message": user_message,
        # Restore collected data
        "collected_name": saved.get("collected_name") or _get_existing_name(user_config),
        "collected_age": saved.get("collected_age") or _get_existing_age(user_config),
        "selected_chapter_id": saved.get("selected_chapter_id"),
        "selected_chapter_name": saved.get("selected_chapter_name"),
        "selected_scenario_id": saved.get("selected_scenario_id"),
        "selected_character_id": saved.get("selected_character_id"),
        # Restore cached lists
        "chapters": saved.get("chapters", []),
        "scenarios": saved.get("scenarios", []),
        # Output fields (will be populated by nodes)
        "reply_text": "",
        "options_to_send": None,
        "onboarding_complete": False,
    }

    logger.info(
        f"Onboarding [{user_id}]: running step='{current_step}' "
        f"msg={repr((user_message or '')[:60])}"
    )

    # Run graph
    graph = _create_onboarding_graph()
    try:
        result = await graph.ainvoke(initial_state)
    except Exception as e:
        logger.error(f"Onboarding graph error for {user_id}: {e}", exc_info=True)
        return {
            "reply_text": "Oops, something went wrong on my end. Let's try again!",
            "options_to_send": None,
            "onboarding_complete": False,
            "selected_scenario_id": None,
            "selected_character_id": None,
        }

    # Persist updated state
    await save_onboarding_state(user_id, result)

    # Detect which step was just completed this turn by comparing before/after state
    step_completed: Optional[str] = None
    step_data: dict = {}

    if not initial_state.get("collected_name") and result.get("collected_name"):
        step_completed = "name_collected"
        step_data = {"name": result["collected_name"]}
    elif not initial_state.get("collected_age") and result.get("collected_age"):
        step_completed = "age_collected"
        step_data = {"age": result["collected_age"]}
    elif not initial_state.get("selected_chapter_id") and result.get("selected_chapter_id"):
        step_completed = "chapter_selected"
        step_data = {
            "chapterId": result["selected_chapter_id"],
            "chapterName": result.get("selected_chapter_name"),
        }
    elif not initial_state.get("selected_scenario_id") and result.get("selected_scenario_id"):
        step_completed = "scenario_selected"
        step_data = {
            "scenarioId": result["selected_scenario_id"],
            "characterId": result.get("selected_character_id"),
        }

    if step_completed:
        logger.info(f"Onboarding [{user_id}]: step completed — {step_completed}")

    return {
        "reply_text": result.get("reply_text", ""),
        "options_to_send": result.get("options_to_send"),
        "onboarding_complete": result.get("onboarding_complete", False),
        "selected_scenario_id": result.get("selected_scenario_id"),
        "selected_character_id": result.get("selected_character_id"),
        "step_completed": step_completed,
        "step_data": step_data,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_existing_name(user_config: dict) -> Optional[str]:
    name = user_config.get("name", "")
    return name if name and name != "Friend" else None


def _get_existing_age(user_config: dict) -> Optional[int]:
    age = user_config.get("age", 0)
    return age if age and age not in (0, 25) else None


def _infer_initial_step(user_config: dict) -> str:
    """
    Determine where to start onboarding based on what we already know.

    - Missing name → start from the very beginning ("init")
    - Has name but missing age → skip to age question ("awaiting_age")
    - Has both → go straight to chapter selection ("awaiting_chapter")
    """
    has_name = bool(_get_existing_name(user_config))
    has_age = bool(_get_existing_age(user_config))

    if not has_name:
        return "init"
    if not has_age:
        return "awaiting_age"
    # Even if we have name+age, always ask for chapter/scenario each session
    return "awaiting_chapter"
