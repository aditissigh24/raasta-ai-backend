"""
Event data models for request validation and response formatting.
"""
import re
from typing import Dict, Any, Optional
from pydantic import BaseModel, field_validator


class EventRequest(BaseModel):
    """Request model for event storage."""
    event_name: str
    event_properties: Dict[str, Any]
    collection_name: str
    distinct_id: Optional[str] = None
    
    @field_validator('event_name')
    @classmethod
    def validate_event_name(cls, v: str) -> str:
        """Validate event_name is not empty."""
        if not v or len(v.strip()) == 0:
            raise ValueError("event_name cannot be empty")
        return v.strip()
    
    @field_validator('collection_name')
    @classmethod
    def validate_collection_name(cls, v: str) -> str:
        """Validate collection_name format (alphanumeric, underscore, hyphen only)."""
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError("Invalid collection_name format. Only alphanumeric, underscore, and hyphen allowed")
        return v
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "event_name": "coach_message_sent_by_coach",
                    "collection_name": "coach_events",
                    "event_properties": {
                        "coach_id": "123",
                        "user_id": "user_abc",
                        "conversation_id": "conv_xyz",
                        "message_id": "msg_123",
                        "message_length": 150,
                        "page_url": "https://example.com/dashboard",
                        "device_type": "desktop",
                        "session_id": "session_123",
                        "timestamp": 1707821400000
                    },
                    "distinct_id": "mixpanel_distinct_id_optional"
                }
            ]
        }
    }


class EventResponse(BaseModel):
    """Success response model for event storage."""
    success: bool = True
    message: str
    event_id: str
    timestamp: str
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "success": True,
                    "message": "Event stored successfully",
                    "event_id": "65cbf1234567890abcdef123",
                    "timestamp": "2026-02-13T10:30:00.000Z"
                }
            ]
        }
    }


class ErrorResponse(BaseModel):
    """Error response model."""
    success: bool = False
    error: str
    code: str
    details: Optional[Dict[str, Any]] = None
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "success": False,
                    "error": "Invalid or missing API key",
                    "code": "UNAUTHORIZED"
                },
                {
                    "success": False,
                    "error": "Invalid event format",
                    "code": "VALIDATION_ERROR",
                    "details": {
                        "event_name": "Required field missing"
                    }
                },
                {
                    "success": False,
                    "error": "Failed to store event",
                    "code": "SERVER_ERROR"
                }
            ]
        }
    }
