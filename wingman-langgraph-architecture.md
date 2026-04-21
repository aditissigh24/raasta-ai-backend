# Wingman — LangGraph + LangChain Architecture

## Philosophy

- **LangGraph** owns the conversation flow — state, routing, node execution
- **LangChain LCEL** owns the LLM calls — chains, fallbacks, prompt composition
- **Langfuse** owns prompt templates and observability
- **Redis** owns the prompt cache
- **Postgres** owns all persistent state

Two graphs:
- `conversation_graph` — blocking, user waits for this
- `background_graph` — async, runs after response is sent

---

## Project Structure

```
wingman/
├── graphs/
│   ├── conversation_graph.py     # main turn graph
│   └── background_graph.py       # eval + beat orchestration
├── nodes/
│   ├── load_context.py
│   ├── assemble_prompt.py
│   ├── conversation_node.py
│   ├── recovery_node.py
│   ├── rephraser_node.py
│   ├── save_output.py
│   ├── engagement_eval_node.py
│   └── beat_orchestrator_node.py
├── chains/
│   ├── character_chain.py        # Deepseek + Claude fallback chain
│   ├── recovery_chain.py         # Claude Sonnet chain
│   ├── rephraser_chain.py        # Deepseek rephrase chain
│   └── eval_chain.py             # Claude Haiku eval chain
├── state/
│   └── wingman_state.py          # TypedDict state definitions
├── prompts/
│   └── langfuse_loader.py        # Langfuse fetch + Redis cache
├── db/
│   └── conversation_repo.py      # all DB queries
└── pipeline.py                   # entry point called by API route
```

---

## State Definitions

**File:** `state/wingman_state.py`

```python
from typing import TypedDict, Optional, Annotated
from langgraph.graph.message import add_messages


class ConversationState(TypedDict):
    """
    Main state passed between all nodes in conversation_graph.
    Every node reads from this and writes back to it.
    LangGraph merges updates automatically after each node.
    """

    # ── Identity ────────────────────────────────────────────────
    conversation_id: str
    user_id: str

    # ── User Input ──────────────────────────────────────────────
    user_message: str

    # ── DB Records (loaded once in load_context node) ───────────
    character: dict                    # full character record from DB
    scenario: dict                     # full scenario record from DB
    current_beat: dict                 # current ScenarioBeat record
    turns_in_current_beat: int
    total_turns: int

    # ── Conversation History ─────────────────────────────────────
    # add_messages reducer — LangGraph appends new messages automatically
    messages: Annotated[list, add_messages]

    # ── Engagement ───────────────────────────────────────────────
    engagement_score: float            # latest score from eval (1.0–5.0)
    inject_hook: bool                  # True when score < beat threshold
    suggested_hook: Optional[str]      # hint from last eval run

    # ── Assembled Prompt ─────────────────────────────────────────
    stable_section: str                # guardrails + character + scene (cached)
    dynamic_section: str               # current beat (fresh every turn)
    system_prompt: str                 # stable + dynamic combined

    # ── Output ───────────────────────────────────────────────────
    raw_response: Optional[str]        # Claude's recovery response (pre-rephrase)
    final_response: Optional[str]      # response sent to user
    model_used: str                    # "deepseek" | "claude-sonnet-fallback"
    was_engagement_triggered: bool     # whether recovery path ran this turn
    langfuse_trace_id: str


class BackgroundState(TypedDict):
    """
    State for background_graph.
    Runs after conversation_graph completes, never blocks user.
    """
    conversation_id: str
    total_turns: int
    turns_in_current_beat: int
    engagement_score: float
    current_beat: dict
    scenario_id: str
    last_n_user_messages: list[str]
    character_name: str
    scenario_title: str

    # outputs
    new_engagement_score: Optional[float]
    suggested_hook: Optional[str]
    beat_advanced: bool
    conversation_completed: bool
```

---

## Conversation Graph

**File:** `graphs/conversation_graph.py`

```
                    ┌─────────────────┐
                    │   load_context  │  loads DB records into state
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ assemble_prompt │  Langfuse fetch + Redis cache
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  route_path     │  conditional edge
                    └────────┬────────┘
               ┌─────────────┴──────────────┐
               │ inject_hook=False           │ inject_hook=True
    ┌──────────▼──────────┐      ┌──────────▼──────────┐
    │  conversation_node  │      │   recovery_node      │
    │  Deepseek + Claude  │      │   Claude Sonnet      │
    │  fallback chain     │      └──────────┬──────────┘
    └──────────┬──────────┘                 │
               │                 ┌──────────▼──────────┐
               │                 │   rephraser_node    │
               │                 │   Deepseek chain    │
               │                 └──────────┬──────────┘
               └─────────────┬──────────────┘
                    ┌────────▼────────┐
                    │   save_output   │  saves messages, updates counters
                    └────────┬────────┘
                             │
                           END
```

```python
from langgraph.graph import StateGraph, END
from state.wingman_state import ConversationState
from nodes.load_context import load_context
from nodes.assemble_prompt import assemble_prompt
from nodes.conversation_node import conversation_node
from nodes.recovery_node import recovery_node
from nodes.rephraser_node import rephraser_node
from nodes.save_output import save_output


def route_path(state: ConversationState) -> str:
    """
    Conditional edge — decides which path to take.
    Returns the name of the next node.
    """
    return "recovery_node" if state["inject_hook"] else "conversation_node"


def build_conversation_graph() -> StateGraph:
    graph = StateGraph(ConversationState)

    # register nodes
    graph.add_node("load_context",       load_context)
    graph.add_node("assemble_prompt",    assemble_prompt)
    graph.add_node("conversation_node",  conversation_node)
    graph.add_node("recovery_node",      recovery_node)
    graph.add_node("rephraser_node",     rephraser_node)
    graph.add_node("save_output",        save_output)

    # entry point
    graph.set_entry_point("load_context")

    # linear edges
    graph.add_edge("load_context",    "assemble_prompt")

    # conditional routing after prompt assembly
    graph.add_conditional_edges(
        "assemble_prompt",
        route_path,
        {
            "conversation_node": "conversation_node",
            "recovery_node":     "recovery_node",
        }
    )

    # normal path exits to save
    graph.add_edge("conversation_node", "save_output")

    # recovery path goes through rephraser before save
    graph.add_edge("recovery_node",     "rephraser_node")
    graph.add_edge("rephraser_node",    "save_output")

    # end
    graph.add_edge("save_output", END)

    return graph.compile()


conversation_graph = build_conversation_graph()
```

---

## Background Graph

**File:** `graphs/background_graph.py`

```
    ┌──────────────────────┐
    │  should_run_eval?    │  conditional — runs every N turns
    └──────────┬───────────┘
          ┌────┴────┐
          │yes      │no
    ┌─────▼──────┐  │
    │ eval_node  │  │   Claude Haiku scores engagement
    └─────┬──────┘  │
          └────┬────┘
    ┌──────────▼───────────┐
    │  beat_orchestrator   │   checks advance conditions, updates beat
    └──────────┬───────────┘
               │
             END
```

```python
from langgraph.graph import StateGraph, END
from state.wingman_state import BackgroundState
from nodes.engagement_eval_node import engagement_eval_node
from nodes.beat_orchestrator_node import beat_orchestrator_node


def should_run_eval(state: BackgroundState) -> str:
    eval_interval = 3  # configurable via env
    turns_since_last_eval = state["total_turns"] % eval_interval
    return "eval_node" if turns_since_last_eval == 0 else "beat_orchestrator"


def build_background_graph() -> StateGraph:
    graph = StateGraph(BackgroundState)

    graph.add_node("eval_node",          engagement_eval_node)
    graph.add_node("beat_orchestrator",  beat_orchestrator_node)

    graph.set_entry_point("should_eval")
    graph.add_node("should_eval", lambda state: state)  # passthrough router node

    graph.add_conditional_edges(
        "should_eval",
        should_run_eval,
        {
            "eval_node":         "eval_node",
            "beat_orchestrator": "beat_orchestrator",
        }
    )

    graph.add_edge("eval_node",         "beat_orchestrator")
    graph.add_edge("beat_orchestrator", END)

    return graph.compile()


background_graph = build_background_graph()
```

---

## LangChain Chains

These are the actual LLM call definitions.
Nodes in the graph call these chains — keeping LLM logic separate from graph logic.

---

### Character Chain (Deepseek + Claude Fallback)

**File:** `chains/character_chain.py`

```python
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI  # Deepseek uses OpenAI-compatible API
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langfuse.callback import CallbackHandler


def build_character_chain(langfuse_trace_id: str):
    """
    Primary: Deepseek
    Fallback: Claude Sonnet (auto-triggered on timeout or API error)
    """

    langfuse_handler = CallbackHandler(trace_id=langfuse_trace_id)

    # Primary — Deepseek via OpenAI-compatible endpoint
    deepseek = ChatOpenAI(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key=settings.DEEPSEEK_API_KEY,
        temperature=0.85,
        timeout=8,                         # triggers fallback after 8s
        max_retries=1,
    )

    # Fallback — Claude Sonnet
    claude_sonnet = ChatAnthropic(
        model="claude-sonnet-4-5",
        temperature=0.85,
        max_tokens=1000,
    )

    # Chain: Deepseek with Claude as fallback
    # .with_fallbacks() is native LangChain — no custom try/catch needed
    primary_model = deepseek.with_fallbacks(
        [claude_sonnet],
        exceptions_to_handle=(TimeoutError, Exception),
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system_prompt}"),
        *[("{role}", "{content}") for msg in []],  # history injected dynamically
        ("human", "{user_message}"),
    ])

    chain = prompt | primary_model | StrOutputParser()

    return chain, langfuse_handler


def build_character_chain_with_prefix_cache(
    stable_section: str,
    dynamic_section: str,
    langfuse_trace_id: str
):
    """
    Anthropic prefix-cache aware version.
    Used when fallback to Claude is triggered —
    passes stable/dynamic sections separately for cache marking.
    """
    from anthropic import Anthropic

    client = Anthropic()

    async def call_with_cache(history: list, user_message: str) -> str:
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=[
                {
                    "type": "text",
                    "text": stable_section,
                    "cache_control": {"type": "ephemeral"},  # cached prefix
                },
                {
                    "type": "text",
                    "text": dynamic_section,               # not cached, changes per beat
                }
            ],
            messages=[
                *history,
                {"role": "user", "content": user_message}
            ],
        )
        return response.content[0].text

    return call_with_cache
```

---

### Recovery Chain (Claude Sonnet)

**File:** `chains/recovery_chain.py`

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langfuse.callback import CallbackHandler


def build_recovery_chain(langfuse_trace_id: str):
    """
    Claude Sonnet only — used on engagement recovery path.
    No fallback needed here. If this fails, skip recovery and use normal path.
    """

    langfuse_handler = CallbackHandler(
        trace_id=langfuse_trace_id,
        observation_id="recovery_node",
    )

    model = ChatAnthropic(
        model="claude-sonnet-4-5",
        temperature=0.9,               # slightly higher — more creative hooks
        max_tokens=800,
        callbacks=[langfuse_handler],
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", "{system_prompt}"),  # includes engagement-injection appended
        ("human",  "{user_message}"),
    ])

    chain = prompt | model | StrOutputParser()
    return chain
```

---

### Rephraser Chain (Deepseek)

**File:** `chains/rephraser_chain.py`

```python
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langfuse.callback import CallbackHandler


def build_rephraser_chain(langfuse_trace_id: str):
    """
    Deepseek only — simple reformatting task, no reasoning required.
    Cheap and fast. No fallback — if it fails, return Claude's raw response.
    """

    langfuse_handler = CallbackHandler(
        trace_id=langfuse_trace_id,
        observation_id="rephraser_node",
    )

    model = ChatOpenAI(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key=settings.DEEPSEEK_API_KEY,
        temperature=0.7,               # lower — stay close to original meaning
        max_tokens=800,
        callbacks=[langfuse_handler],
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", "{rephraser_system_prompt}"),
        ("human",  "Rewrite this: {response_to_rephrase}"),
    ])

    chain = prompt | model | StrOutputParser()
    return chain
```

---

### Eval Chain (Claude Haiku)

**File:** `chains/eval_chain.py`

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langfuse.callback import CallbackHandler


def build_eval_chain(langfuse_trace_id: str):
    """
    Claude Haiku — fastest and cheapest model.
    Returns structured JSON — use JsonOutputParser directly.
    """

    langfuse_handler = CallbackHandler(
        trace_id=langfuse_trace_id,
        observation_id="engagement_eval",
    )

    model = ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        temperature=0,                  # zero temp for consistent scoring
        max_tokens=200,                 # score + reason + hook fits in 200 tokens
        callbacks=[langfuse_handler],
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", "{eval_system_prompt}"),
        ("human",  "Messages to evaluate:\n{messages}"),
    ])

    # JsonOutputParser handles parsing + validation automatically
    chain = prompt | model | JsonOutputParser()
    return chain
```

---

## Nodes

Each node is a pure async function: takes state, returns partial state update.
LangGraph merges the returned dict back into the full state.

---

### Node 1 — Load Context

**File:** `nodes/load_context.py`

```python
async def load_context(state: ConversationState) -> dict:
    """
    Single DB query with joins.
    Loads everything needed for the turn into state.
    """
    conversation = await conversation_repo.load_full(
        state["conversation_id"],
        include_history_limit=12,
    )

    inject_hook = (
        conversation.engagement_score <
        conversation.current_beat.engaged_advance_score
    )

    return {
        "character":             conversation.scenario.character.__dict__,
        "scenario":              conversation.scenario.__dict__,
        "current_beat":          conversation.current_beat.__dict__,
        "turns_in_current_beat": conversation.turns_in_current_beat,
        "total_turns":           conversation.total_turns,
        "engagement_score":      conversation.engagement_score,
        "suggested_hook":        conversation.last_eval_suggested_hook,
        "inject_hook":           inject_hook,
        "messages":              conversation.history,   # LangGraph appends via reducer
        "langfuse_trace_id":     generate_trace_id(),
    }
```

---

### Node 2 — Assemble Prompt

**File:** `nodes/assemble_prompt.py`

```python
async def assemble_prompt(state: ConversationState) -> dict:
    """
    Builds system prompt from Langfuse templates + DB data.
    Stable section pulled from Redis cache.
    Beat section always freshly compiled.
    """
    character = state["character"]
    scenario  = state["scenario"]
    beat      = state["current_beat"]

    # ── Stable section (Redis cached) ────────────────────────────
    cache_key = f"prompt:base:{character['id']}:{scenario['id']}"
    stable_section = await redis.get(cache_key)

    if not stable_section:
        guardrails_tpl, character_tpl, scene_tpl = await asyncio.gather(
            langfuse_loader.get("wingman/base/guardrails"),
            langfuse_loader.get("wingman/base/character-persona"),
            langfuse_loader.get("wingman/base/scene-directive"),
        )

        stable_section = "\n\n---\n\n".join([
            guardrails_tpl.compile({
                "character_name": character["name"],
                "hard_limits":    ", ".join(character["hard_limits"]),
            }),
            character_tpl.compile({
                "character_name":    character["name"],
                "age":               character["age"],
                "city":              character["city"],
                "archetype":         character["archetype"],
                "personality_traits": character["vibe_summary"],
                "backstory":         character["backstory"],
                "speaking_style":    character["speaking_style"],
                "emoji_usage":       character["emoji_usage"],
                "texting_speed":     character["texting_speed"],
                "voice_prompt":      character["voice_prompt"],
            }),
            scene_tpl.compile({
                "scenario_title":      scenario["scenario_title"],
                "setting_description": scenario["setting_description"],
                "time_of_day":         scenario["time_of_day"],
                "atmosphere":          scenario["atmosphere"],
                "tone":                scenario["tone"],
                "overall_arc":         scenario["overall_arc"],
            }),
        ])

        await redis.set(cache_key, stable_section, ex=600)

    # ── Dynamic section (never cached) ───────────────────────────
    beat_tpl = await langfuse_loader.get("wingman/base/current-beat")

    # pick directive based on engagement
    directive = (
        beat["hook_directive"]
        if state["inject_hook"]
        else beat["flow_directive"]
    )

    # optionally append engagement injection
    if state["inject_hook"]:
        injection_tpl = await langfuse_loader.get("wingman/base/engagement-injection")
        injection     = injection_tpl.compile({"character_name": character["name"]})
        directive     = f"{directive}\n\n{injection}"

    dynamic_section = beat_tpl.compile({
        "narrative_context":         beat["narrative_context"],
        "character_emotional_state": beat["character_emotional_state"],
        "directive":                 directive,
    })

    system_prompt = f"{stable_section}\n\n---\n\n{dynamic_section}"

    return {
        "stable_section":  stable_section,
        "dynamic_section": dynamic_section,
        "system_prompt":   system_prompt,
    }
```

---

### Node 3 — Conversation Node

**File:** `nodes/conversation_node.py`

```python
async def conversation_node(state: ConversationState) -> dict:
    """
    Normal path — Deepseek with Claude Sonnet fallback.
    """
    chain, handler = build_character_chain(state["langfuse_trace_id"])

    response = await chain.ainvoke({
        "system_prompt": state["system_prompt"],
        "user_message":  state["user_message"],
        "messages":      state["messages"],
    }, config={"callbacks": [handler]})

    return {
        "final_response":           response,
        "was_engagement_triggered": False,
        "model_used":               "deepseek",
    }
```

---

### Node 4 — Recovery Node

**File:** `nodes/recovery_node.py`

```python
async def recovery_node(state: ConversationState) -> dict:
    """
    Recovery path — Claude Sonnet generates the engagement hook.
    Output goes to rephraser, not directly to user.
    """
    chain = build_recovery_chain(state["langfuse_trace_id"])

    response = await chain.ainvoke({
        "system_prompt": state["system_prompt"],  # includes engagement-injection
        "user_message":  state["user_message"],
    })

    return {
        "raw_response":             response,   # not final — rephraser processes this
        "was_engagement_triggered": True,
    }
```

---

### Node 5 — Rephraser Node

**File:** `nodes/rephraser_node.py`

```python
async def rephraser_node(state: ConversationState) -> dict:
    """
    Recovery path only — rewrites Claude's response in character voice.
    If Deepseek fails, fall back to Claude's raw response directly.
    """
    character = state["character"]

    rephraser_tpl = await langfuse_loader.get("wingman/base/rephraser")
    rephraser_system = rephraser_tpl.compile({
        "character_name": character["name"],
        "voice_prompt":   character["voice_prompt"],
        "response_to_rephrase": state["raw_response"],
    })

    try:
        chain    = build_rephraser_chain(state["langfuse_trace_id"])
        response = await chain.ainvoke({
            "rephraser_system_prompt": rephraser_system,
            "response_to_rephrase":    state["raw_response"],
        })
    except Exception:
        # rephraser failed — use Claude's raw response, still better than nothing
        response = state["raw_response"]

    return {
        "final_response": response,
        "model_used":     "deepseek-rephraser",
    }
```

---

### Node 6 — Save Output

**File:** `nodes/save_output.py`

```python
async def save_output(state: ConversationState) -> dict:
    """
    Persists user message + assistant response.
    Increments turn counters on Conversation record.
    """
    turn_number = state["total_turns"] + 1

    await asyncio.gather(
        conversation_repo.save_message(
            conversation_id=state["conversation_id"],
            role="user",
            content=state["user_message"],
            turn_number=turn_number,
            beat_number=state["current_beat"]["beat_number"],
        ),
        conversation_repo.save_message(
            conversation_id=state["conversation_id"],
            role="assistant",
            content=state["final_response"],
            turn_number=turn_number,
            beat_number=state["current_beat"]["beat_number"],
            was_engagement_triggered=state["was_engagement_triggered"],
            langfuse_trace_id=state["langfuse_trace_id"],
        ),
        conversation_repo.increment_turns(state["conversation_id"]),
    )

    return {}   # no state update needed — side effects only
```

---

### Node 7 — Engagement Eval Node

**File:** `nodes/engagement_eval_node.py`

```python
async def engagement_eval_node(state: BackgroundState) -> dict:
    """
    Scores user engagement using Claude Haiku.
    Runs in background graph — never blocks user.
    """
    eval_tpl = await langfuse_loader.get("wingman/eval/engagement-scorer")
    eval_system = eval_tpl.compile({
        "character_name":    state["character_name"],
        "scenario_title":    state["scenario_title"],
        "beat_type":         state["current_beat"]["beat_type"],
        "narrative_context": state["current_beat"]["narrative_context"],
        "n":                 len(state["last_n_user_messages"]),
    })

    chain  = build_eval_chain(state.get("langfuse_trace_id", ""))
    result = await chain.ainvoke({
        "eval_system_prompt": eval_system,
        "messages": "\n".join(
            f"{i+1}. {msg}"
            for i, msg in enumerate(state["last_n_user_messages"])
        ),
    })

    # result is already parsed JSON from JsonOutputParser
    score  = float(result["score"])
    reason = result["reason"]
    hook   = result.get("suggested_hook")

    # persist to DB
    await conversation_repo.update_engagement(
        conversation_id=state["conversation_id"],
        score=score,
        suggested_hook=hook,
        last_eval_at=state["total_turns"],
    )

    return {
        "new_engagement_score": score,
        "suggested_hook":       hook,
    }
```

---

### Node 8 — Beat Orchestrator Node

**File:** `nodes/beat_orchestrator_node.py`

```python
async def beat_orchestrator_node(state: BackgroundState) -> dict:
    """
    Checks if beat should advance.
    Updates Conversation record if so.
    Marks conversation completed if on final beat.
    """
    beat  = state["current_beat"]
    score = state.get("new_engagement_score", state["engagement_score"])

    should_advance = (
        state["turns_in_current_beat"] >= beat["min_turns_in_beat"] and
        score >= beat["engaged_advance_score"]
    )

    if not should_advance:
        return {"beat_advanced": False, "conversation_completed": False}

    # fetch next beat
    next_beat = await conversation_repo.get_next_beat(
        scenario_id=state["scenario_id"],
        current_beat_number=beat["beat_number"],
    )

    # final beat completed — mark conversation done
    if next_beat is None:
        await conversation_repo.complete_conversation(state["conversation_id"])
        return {"beat_advanced": True, "conversation_completed": True}

    # advance to next beat
    await conversation_repo.advance_beat(
        conversation_id=state["conversation_id"],
        new_beat_id=next_beat["id"],
        new_beat_number=next_beat["beat_number"],
    )

    return {"beat_advanced": True, "conversation_completed": False}
```

---

## Pipeline Entry Point

**File:** `pipeline.py`

```python
import asyncio
from graphs.conversation_graph import conversation_graph
from graphs.background_graph import background_graph
from db.conversation_repo import conversation_repo


async def handle_turn(conversation_id: str, user_message: str) -> str:
    """
    Single entry point called by your API route.
    Returns the character's response string.
    """

    # ── Run conversation graph (blocking — user waits) ────────────
    result = await conversation_graph.ainvoke({
        "conversation_id": conversation_id,
        "user_message":    user_message,
    })

    final_response = result["final_response"]

    # ── Fire background graph (non-blocking — user doesn't wait) ──
    asyncio.create_task(
        run_background(conversation_id, result)
    )

    return final_response


async def run_background(conversation_id: str, conversation_result: dict):
    """
    Runs after response is sent.
    Handles eval + beat advancement.
    """
    last_n_messages = await conversation_repo.get_last_n_user_messages(
        conversation_id, n=3
    )

    await background_graph.ainvoke({
        "conversation_id":          conversation_id,
        "total_turns":              conversation_result["total_turns"] + 1,
        "turns_in_current_beat":    conversation_result["turns_in_current_beat"] + 1,
        "engagement_score":         conversation_result["engagement_score"],
        "current_beat":             conversation_result["current_beat"],
        "scenario_id":              conversation_result["scenario"]["id"],
        "last_n_user_messages":     last_n_messages,
        "character_name":           conversation_result["character"]["name"],
        "scenario_title":           conversation_result["scenario"]["scenario_title"],
    })
```

---

## Langfuse Integration

Langfuse wraps the entire turn as one trace with child spans per node.

```python
# prompts/langfuse_loader.py
from langfuse import Langfuse
from redis import Redis

langfuse = Langfuse()
redis    = Redis.from_url(settings.REDIS_URL)

TEMPLATE_CACHE_TTL = 600  # 10 minutes

async def get(prompt_name: str):
    """
    Fetch prompt template from Redis cache.
    On miss — fetch from Langfuse and cache.
    """
    cache_key = f"langfuse:template:{prompt_name}"
    cached    = redis.get(cache_key)

    if cached:
        return PromptTemplate.from_json(cached)

    template = langfuse.get_prompt(prompt_name, label="production")
    redis.set(cache_key, template.to_json(), ex=TEMPLATE_CACHE_TTL)
    return template
```

Each LLM call gets a `CallbackHandler` tied to the same `trace_id` so you see
the full turn — prompt assembly, model call, tokens, latency — in one Langfuse trace.

---

## Full Data Flow Per Turn

```
API Route receives user message
         │
         ▼
pipeline.handle_turn()
         │
         ▼
conversation_graph.ainvoke()
    │
    ├── load_context        → DB query → state populated
    ├── assemble_prompt     → Redis / Langfuse → system_prompt built
    ├── route_path          → engagement_score checked
    │
    ├── [normal path]
    │   └── conversation_node  → Deepseek (Claude fallback) → final_response
    │
    └── [recovery path]
        ├── recovery_node   → Claude Sonnet → raw_response
        └── rephraser_node  → Deepseek → final_response
    │
    └── save_output         → DB write → done
         │
         ▼
    return final_response to API route → sent to user
         │
         ▼ (non-blocking)
background_graph.ainvoke()
    ├── engagement_eval_node   → Haiku → score updated in DB
    └── beat_orchestrator_node → beat advanced if conditions met
```

---

## Environment Variables

```bash
# Models
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
ANTHROPIC_API_KEY=

# Langfuse
LANGFUSE_SECRET_KEY=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# Cache + DB
REDIS_URL=
DATABASE_URL=

# Tuning
EVAL_INTERVAL_TURNS=3
MAX_HISTORY_TURNS=12
DEEPSEEK_TIMEOUT_SECONDS=8
```
