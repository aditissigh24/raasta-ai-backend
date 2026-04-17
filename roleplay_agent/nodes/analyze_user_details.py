"""
Analyzing agent node for extracting user details from messages.
"""
import logging
from typing import Optional, Dict, List, Literal
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field
from config.settings import settings

logger = logging.getLogger(__name__)


class UserDetailsExtraction(BaseModel):
    """Schema for extracted user details."""
    name: Optional[str] = Field(None, description="User's first name or preferred name")
    gender: Optional[Literal["MALE", "FEMALE", "NON_BINARY", "PREFER_NOT_SAY"]] = Field(None, description="User's gender")
    age: Optional[int] = Field(None, description="User's age in years")
    currentSituation: Optional[str] = Field(None, description="User's current relationship situation or issue")
    situations: Optional[List[str]] = Field(None, description="List of relationship situations or topics")


def get_missing_fields(user_config: dict) -> List[str]:
    """
    Identify which fields are missing or have default values in user_config.

    Args:
        user_config: User configuration dictionary

    Returns:
        List of field names that are missing
    """
    missing = []

    name = user_config.get("name", "")
    if not name or name == "Friend":
        missing.append("name")

    gender = user_config.get("gender", "")
    if not gender or gender == "unknown":
        missing.append("gender")

    age = user_config.get("age", 25)
    if age == 25 or age == 0:
        missing.append("age")

    current_situation = user_config.get("currentSituation", "")
    if not current_situation:
        missing.append("currentSituation")

    situations = user_config.get("situations", [])
    if not situations:
        missing.append("situations")

    return missing


async def analyze_user_details(
    user_message: str,
    user_config: dict,
    missing_fields: List[str]
) -> Optional[Dict]:
    """
    Analyze user message to extract missing user details using LLM.

    Args:
        user_message: The user's message text
        user_config: Current user configuration
        missing_fields: List of fields that need to be extracted

    Returns:
        Dictionary of extracted fields, or None if extraction failed
    """
    if not missing_fields:
        logger.info("No missing fields to extract")
        return None

    try:
        llm = ChatOpenAI(
            model=settings.OPENAI_MODEL,
            temperature=0,
            api_key=settings.OPENAI_API_KEY
        )

        structured_llm = llm.with_structured_output(UserDetailsExtraction)

        system_prompt = f"""You are analyzing a user's message to extract personal information for their profile.

Current user profile:
- Name: {user_config.get('name', 'Unknown')}
- Gender: {user_config.get('gender', 'Unknown')}
- Age: {user_config.get('age', 'Unknown')}
- Current Situation: {user_config.get('currentSituation', 'Unknown')}
- Situations: {user_config.get('situations', [])}

Missing fields that need extraction: {', '.join(missing_fields)}

Your task:
1. Extract ONLY the missing information from the user's message
2. Be conservative - only extract information that is explicitly mentioned
3. For gender: use exactly one of "MALE", "FEMALE", "NON_BINARY", "PREFER_NOT_SAY"
4. For age: extract the numeric age if mentioned
5. For currentSituation: extract a brief description of their relationship issue or situation
6. For situations: extract relevant relationship topics/issues as a list

If a field is not mentioned in the message, leave it as null.
"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"User message: {user_message}")
        ]

        result = structured_llm.invoke(messages)

        extracted = {}

        if "name" in missing_fields and result.name:
            extracted["name"] = result.name

        if "gender" in missing_fields and result.gender:
            extracted["gender"] = result.gender

        if "age" in missing_fields and result.age:
            extracted["ageRange"] = result.age

        if "currentSituation" in missing_fields and result.currentSituation:
            extracted["currentSituation"] = result.currentSituation

        if "situations" in missing_fields and result.situations:
            extracted["situations"] = result.situations

        if extracted:
            logger.info(f"Extracted user details: {list(extracted.keys())}")
            return extracted
        else:
            logger.info("No user details found in message")
            return None

    except Exception as e:
        logger.error(f"Error analyzing user details: {e}")
        return None
