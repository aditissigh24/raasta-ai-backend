"""
Node for fetching user configuration from the backend service.
"""
from coach_agent.state import CoachState
from services.backend_client import backend_client


async def fetch_configuration_node(state: CoachState) -> dict:
    """
    Fetch user configuration from the backend service.
    
    This node is called at the start of a new session to get
    user information (name, gender, age) from the backend.
    
    Args:
        state: The current conversation state
        
    Returns:
        Updated state with user configuration
    """
    # If config is already fetched, skip
    if state.get("config_fetched", False):
        return {}
    
    user_id = state["user_config"].get("user_id", "")
    
    if not user_id:
        # No user_id provided, use defaults
        return {
            "user_config": {
                "user_id": "unknown",
                "name": "Friend",
                "gender": "unknown",
                "age": 25
            },
            "config_fetched": True
        }
    
    try:
        # Fetch user config from backend
        user_config = await backend_client.fetch_user_config(user_id)
        
        return {
            "user_config": user_config,
            "config_fetched": True
        }
    except Exception as e:
        # On error, use defaults with user_id preserved
        print(f"Error fetching user config: {e}")
        return {
            "user_config": {
                "user_id": user_id,
                "name": "Friend",
                "gender": "unknown",
                "age": 25
            },
            "config_fetched": True
        }


