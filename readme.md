## Quick Start

### 1. Install Dependencies

```bash
# Activate virtual environment (if using one)
source venv/bin/activate  # Linux/Mac

# Install/update dependencies
pip install -r requirements.txt
```
### 3. Run the Application

```bash
python main.py
```

Or with uvicorn:

```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

## Architecture

```
┌─────────────┐         ┌─────────────────┐         ┌─────────────────┐
│  User App   │◀───────▶│  Socket.io      │────────▶│  Redis Pub/Sub  │
│  (Next.js)  │ Socket  │  Server         │ publish  │                 │
│             │         │                 │◀────────│                 │
└─────────────┘         └─────────────────┘ subscribe└────────┬────────┘
                                                              │
                                                     subscribe│publish
                                                              │
                                                     ┌────────▼────────┐
                                                     │  Python Service  │
                                                     │  (FastAPI + LLM) │
                                                     └─────────────────┘
```

### Channels

- `ai:request` — Socket server publishes user messages that need AI responses
- `ai:response` — AI backend publishes LLM replies
- `ai:disconnect` — Socket server notifies on user disconnect for session summarization
