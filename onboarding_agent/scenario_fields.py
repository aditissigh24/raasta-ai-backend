"""Normalize scenario dict keys from the roleplay API (snake_case vs camelCase)."""


def character_id_from_scenario(sc: dict) -> str:
    """Return linked character id; API may expose char_id, character_id, or characterId."""
    v = sc.get("char_id") or sc.get("character_id") or sc.get("characterId")
    if v is None:
        return ""
    s = str(v).strip()
    return s
