"""
Wingman — LangGraph + LangChain conversation engine.

Hot path: pipeline.handle_turn_streaming() → Redis-first context → LLM stream → Socket.IO tokens
Background: background_graph (LangGraph) → engagement eval → beat orchestration
"""
