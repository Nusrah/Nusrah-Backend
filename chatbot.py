"""
chatbot.py — the Nusrah chatbot API endpoint.

POST /api/chat   body: {"message": "show masjid campaigns", "history": [{"role": "user", "content": "..."}]}

How a question is answered:
  1. chatbot_rules.answer()  -> instant, free, no API key. Handles most questions.
  2. chatbot_llm.ask_llm()   -> AI fallback (Gemini free tier) for everything the rules don't understand.
  3. A friendly default reply if the AI is unavailable, disabled, or over its daily cap.

The AI part is optional: if chatbot_llm.py is empty, missing, or has no API key,
the chatbot still works using the rules alone.

Protection for a free-tier AI quota:
  - 20 requests per minute per visitor (IP address)
  - a daily cap on AI calls (CHATBOT_AI_DAILY_LIMIT, default 200)
  - messages longer than 500 characters are rejected, history is trimmed
"""

import os
import time
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import chatbot_rules

try:
    import chatbot_llm  # AI fallback (added in the next step)
except Exception:  # file empty/missing, or its library isn't installed yet
    chatbot_llm = None

router = APIRouter(prefix="/api/chat", tags=["chatbot"])

# --- Limits -----------------------------------------------------------------
MAX_MESSAGE_CHARS = 500
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 1000

RATE_WINDOW_SECONDS = 60
RATE_MAX_REQUESTS = 20  # per visitor per window

AI_DAILY_LIMIT = int(os.getenv("CHATBOT_AI_DAILY_LIMIT", "200"))

_requests: Dict[str, List[float]] = defaultdict(list)
_ai_usage = {"day": date.today(), "count": 0}


# --- Request model ------------------------------------------------------------
class ChatTurn(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: List[ChatTurn] = []


# --- Helpers ------------------------------------------------------------------
def _rate_limited(visitor: str) -> bool:
    now = time.time()
    recent = [t for t in _requests[visitor] if now - t < RATE_WINDOW_SECONDS]
    if len(recent) >= RATE_MAX_REQUESTS:
        _requests[visitor] = recent
        return True
    recent.append(now)
    _requests[visitor] = recent

    # keep memory small: forget visitors who haven't chatted recently
    if len(_requests) > 5000:
        for key in [k for k, v in _requests.items() if not v or now - v[-1] > RATE_WINDOW_SECONDS]:
            _requests.pop(key, None)
    return False


def _ai_budget_available() -> bool:
    today = date.today()
    if _ai_usage["day"] != today:
        _ai_usage["day"] = today
        _ai_usage["count"] = 0
    return _ai_usage["count"] < AI_DAILY_LIMIT


def _clean_history(history: List[ChatTurn]) -> List[Dict[str, str]]:
    cleaned = []
    for turn in history[-MAX_HISTORY_MESSAGES:]:
        role = "assistant" if turn.role == "assistant" else "user"
        content = (turn.content or "").strip()[:MAX_HISTORY_CHARS]
        if content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _default_reply() -> Dict[str, Any]:
    return {
        "reply": (
            "I'm not sure I understood that. I can help you find campaigns, explain how to donate or start a campaign, "
            "point you to the Zakah calculator, or take you to any page. You can also contact our team directly."
        ),
        "campaigns": [],
        "links": [{"label": "Contact us", "path": "/contact"}],
        "suggestions": [
            "Show active campaigns",
            "How do I donate?",
            "How do I start a campaign?",
        ],
        "source": "default",
    }


# --- Endpoint -----------------------------------------------------------------
@router.post("")
def chat(data: ChatRequest, request: Request):
    visitor = request.client.host if request.client else "unknown"
    if _rate_limited(visitor):
        raise HTTPException(
            status_code=429,
            detail="You're sending messages very quickly. Please wait a moment and try again.",
        )

    message = data.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message cannot be empty")

    # 1) Rule-based answer: free and instant
    try:
        rule_reply = chatbot_rules.answer(message)
    except Exception as e:
        print(f"[chatbot] rules error: {e}")
        rule_reply = None
    if rule_reply is not None:
        return rule_reply

    # 2) AI fallback for anything the rules didn't understand
    ask_llm = getattr(chatbot_llm, "ask_llm", None) if chatbot_llm else None
    if ask_llm and _ai_budget_available():
        try:
            ai_reply = ask_llm(message, _clean_history(data.history))
            if ai_reply:
                _ai_usage["count"] += 1
                return ai_reply
        except Exception as e:
            # never show raw errors (they can contain keys or internal details) to visitors
            print(f"[chatbot] AI fallback error: {e}")

    # 3) Friendly default
    return _default_reply()