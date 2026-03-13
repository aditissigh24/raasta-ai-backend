"""
Event storage API routes.
"""
import logging
from datetime import datetime
from typing import Dict, Any
from fastapi import APIRouter, Request, Depends, HTTPException
from pymongo.errors import PyMongoError

from app.models.event import EventRequest, EventResponse, ErrorResponse
from app.middleware.auth import verify_api_key
from config.database import get_collection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.post(
    "/events",
    response_model=EventResponse,
    status_code=201,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized - Invalid or missing API key"},
        400: {"model": ErrorResponse, "description": "Bad Request - Invalid event format"},
        500: {"model": ErrorResponse, "description": "Internal Server Error - Failed to store event"}
    },
    summary="Store Event",
    description="""
    Store event data in MongoDB with dynamic collection support.
    
    This endpoint accepts event data from multiple platforms (coach and user) 
    and stores them in separate MongoDB collections based on the collection_name field.
    
    **Authentication**: Requires X-API-Key header
    
    **Server-side Enrichment**: 
    - Adds server timestamp
    - Captures client IP address
    - Logs user agent
    """
)
async def store_event(
    event: EventRequest,
    request: Request,
    authenticated: bool = Depends(verify_api_key)
) -> EventResponse:
    """
    Store event data in MongoDB.
    
    Args:
        event: Event data including name, properties, and collection
        request: FastAPI request object (for metadata)
        authenticated: Authentication status from dependency
        
    Returns:
        EventResponse with success status, event_id, and timestamp
        
    Raises:
        HTTPException: 500 if database operation fails
    """
    try:
        # Prepare event document with server-side enrichment
        server_timestamp = datetime.utcnow()
        event_document: Dict[str, Any] = {
            "event_name": event.event_name,
            "event_properties": event.event_properties,
            "distinct_id": event.distinct_id,
            "server_timestamp": server_timestamp,
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
            "created_at": server_timestamp
        }
        
        # Get MongoDB collection dynamically
        collection = await get_collection(event.collection_name)
        
        # Insert event into MongoDB
        result = await collection.insert_one(event_document)
        
        # Log successful storage
        logger.info(
            f"Event stored successfully: {event.event_name} "
            f"in collection '{event.collection_name}' "
            f"with ID {result.inserted_id}"
        )
        
        # Return success response
        return EventResponse(
            success=True,
            message="Event stored successfully",
            event_id=str(result.inserted_id),
            timestamp=server_timestamp.isoformat() + "Z"
        )
        
    except RuntimeError as e:
        # MongoDB not connected
        logger.error(f"MongoDB connection error: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": "Database connection unavailable",
                "code": "SERVER_ERROR"
            }
        )
    
    except PyMongoError as e:
        # MongoDB operation error
        logger.error(f"MongoDB operation error while storing event: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": "Failed to store event in database",
                "code": "SERVER_ERROR"
            }
        )
    
    except Exception as e:
        # Unexpected error
        logger.error(f"Unexpected error storing event: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": "Failed to store event",
                "code": "SERVER_ERROR"
            }
        )


@router.get(
    "/events/health",
    tags=["health"],
    summary="Event API Health Check",
    description="Check if the event storage API and MongoDB connection are healthy"
)
async def event_api_health() -> Dict[str, Any]:
    """
    Health check endpoint for event API.
    
    Returns:
        Health status including MongoDB connectivity
    """
    from config.database import mongodb_client
    
    mongo_healthy = await mongodb_client.health_check()
    
    return {
        "status": "healthy" if mongo_healthy else "degraded",
        "mongodb_connected": mongo_healthy,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
