"""
Authentication middleware for API key verification.
"""
import logging
from typing import Optional
from fastapi import Header, HTTPException

from config.settings import settings

logger = logging.getLogger(__name__)


async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    """
    Dependency to verify X-API-Key header.
    
    Args:
        x_api_key: API key from X-API-Key header
        
    Returns:
        True if authenticated
        
    Raises:
        HTTPException: 401 if API key is missing or invalid
    """
    # Check if API key is provided
    if not x_api_key:
        logger.warning("API request without API key")
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": "Missing API key",
                "code": "UNAUTHORIZED"
            }
        )
    
    # Get allowed keys from settings
    allowed_keys_str = settings.EVENT_API_KEYS
    if not allowed_keys_str:
        logger.error("EVENT_API_KEYS not configured in settings")
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": "API authentication not configured",
                "code": "SERVER_ERROR"
            }
        )
    
    # Parse comma-separated keys
    allowed_keys = [key.strip() for key in allowed_keys_str.split(",") if key.strip()]
    
    # Verify the provided key
    if x_api_key not in allowed_keys:
        logger.warning(f"Invalid API key attempted: {x_api_key[:10]}...")
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": "Invalid or missing API key",
                "code": "UNAUTHORIZED"
            }
        )
    
    logger.debug(f"API key authenticated successfully")
    return True
