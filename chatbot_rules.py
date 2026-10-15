"""
chatbot_rules.py — the rule-based half of the Nusrah chatbot.

Free, instant, and needs no API key. It understands common questions
(navigation, how to donate, how to start a campaign, zakah, login, contact...)
and searches active campaigns by topic, location or status.

answer(message) returns a reply dict, or None when it doesn't understand the
question. chatbot.py then hands those questions to the AI fallback.

Reply shape (what the Angular chat widget receives):
{
  "reply": "text to show",
  "campaigns": [ {id, title, location, category, goal, raised, progress, image_url, path, ...} ],
  "links": [ {"label": "Browse all campaigns", "path": "/campaigns"} ],
  "suggestions": ["quick reply chip", ...],
  "source": "rules"
}

SECURITY: only public campaign columns are ever selected here. Bank details,
UPI IDs, verification documents and user ids are never read by the chatbot.
"""

import re
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from database import supabase

# --------------------------------------------------------------------------
# Campaign data (public fields only, cached briefly so the chat stays fast)
# --------------------------------------------------------------------------

# Column sets to try, best first. Your table may not have every optional column (for example
# end_date), so instead of failing we fall back to a smaller set of public columns.
_COLUMN_SETS = [
    "id, title, location, category, description, goal, raised, image_url, end_date, is_featured",
    "id, title, location, category, description, goal, raised, image_url, is_featured",
    "id, title, location, category, description, goal, raised, image_url",
    "id, title, location, description, goal, raised, image_url",
]
_working_columns: Optional[str] = None  # remembered after the first successful query
_CACHE_TTL_SECONDS = 60
_cache: Dict[str, Any] = {"time": 0.0, "data": []}


def _tokens(text: Optional[str]) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _progress(raised: Any, goal: Any) -> int:
    try:
        g = float(goal or 0)
        r = float(raised or 0)
    except (TypeError, ValueError):
        return 0
    if g <= 0:
        return 0
    return max(0, min(100, round(r / g * 100)))


def _prepare(row: Dict[str, Any]) -> Dict[str, Any]:
    """Adds private search helpers (keys starting with '_') to a campaign row."""
    return {
        "id": row.get("id"),
        "title": row.get("title") or "Untitled campaign",
        "location": row.get("location") or "",
        "category": row.get("category") or "",
        "goal": row.get("goal") or 0,
        "raised": row.get("raised") or 0,
        "progress": _progress(row.get("raised"), row.get("goal")),
        "image_url": row.get("image_url"),
        "end_date": row.get("end_date"),
        "is_featured": bool(row.get("is_featured")),
        "path": f"/donate/{row.get('id')}",
        "_text_tokens": _tokens(
            f"{row.get('title') or ''} {row.get('category') or ''} {row.get('location') or ''} {row.get('description') or ''}"
        ),
        "_location_tokens": _tokens(row.get("location")),
    }


def _public_view(campaign: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in campaign.items() if not k.startswith("_")}


def list_active_campaigns() -> List[Dict[str, Any]]:
    """All active campaigns (public fields). Cached for a minute."""
    global _working_columns
    now = time.time()
    if _cache["data"] and now - _cache["time"] < _CACHE_TTL_SECONDS:
        return _cache["data"]

    rows = None
    last_error: Optional[Exception] = None
    for columns in ([_working_columns] if _working_columns else _COLUMN_SETS):
        try:
            res = supabase.table("campaigns").select(columns).ilike("status", "active").execute()
            rows = res.data or []
            _working_columns = columns
            break
        except Exception as e:
            last_error = e
            print(f"[chatbot_rules] campaign query failed with columns '{columns}': {e}")
            # only try a smaller column set when the problem is a missing column
            if "42703" not in str(e) and "does not exist" not in str(e):
                raise
    if rows is None:
        raise last_error if last_error else RuntimeError("Could not load campaigns")

    _cache["data"] = [_prepare(r) for r in rows]
    _cache["time"] = now
    return _cache["data"]


# --------------------------------------------------------------------------
# Understanding the question
# --------------------------------------------------------------------------

# Campaign topics → words people use for them. Categories in your database are
# free text, so we match these words against title, category, location and description.
TOPICS: Dict[str, List[str]] = {
    "masjid": ["masjid", "masjids", "mosque", "mosques", "masajid", "musalla", "eidgah"],
    "orphan": ["orphan", "orphans", "orphanage", "orphanages", "yateem", "yatim", "children", "child", "kids"],
    "education": ["school", "schools", "education", "madrasa", "madrasah", "madarsa", "students", "scholarship", "college", "study"],
    "medical": ["medical", "medicine", "health", "hospital", "treatment", "surgery", "patient", "patients", "cancer", "dialysis", "operation", "illness"],
    "food": ["food", "ration", "rations", "hunger", "meal", "meals", "iftar", "groceries", "kitchen"],
    "water": ["water", "well", "borewell", "handpump"],
    "construction": ["construction", "build", "building", "renovation", "repair", "land"],
    "emergency": ["flood", "floods", "disaster", "emergency", "relief", "earthquake", "fire", "accident"],
    "poor": ["poor", "needy", "widow", "widows", "family", "families", "poverty", "debt", "marriage", "wedding"],
}

TOPIC_LABELS: Dict[str, str] = {
    "masjid": "Masjid",
    "orphan": "orphan care",
    "education": "education",
    "medical": "medical",
    "food": "food relief",
    "water": "clean water",
    "construction": "construction",
    "emergency": "emergency relief",
    "poor": "family support",
}

_STOPWORDS = {
    "the", "a", "an", "my", "your", "our", "this", "that", "these", "those", "me", "i", "we", "you", "it",
    "is", "are", "was", "be", "do", "does", "can", "could", "would", "will", "should", "to", "of", "for",
    "and", "or", "with", "on", "please", "pls", "now", "today", "any", "some", "all", "campaign", "campaigns",
    "category", "type", "kind", "cause", "causes", "project", "projects", "available", "active", "show",
    "find", "search", "list", "give", "tell", "about", "which", "what", "where", "there", "here", "have",
    "has", "want", "need", "looking", "look", "help", "support", "donate", "donation", "donations",
}

_SEARCH_VERBS = {
    "show", "find", "search", "list", "looking", "look", "any", "available", "recommend", "suggest",
    "display", "see", "browse", "which", "what", "want", "need", "give", "tell",
}

_GREETING_WORDS = {
    "hi", "hello", "hey", "hii", "heyy", "salam", "salaam", "assalamualaikum", "assalamu", "alaikum",
    "alaykum", "aslam", "wa", "good", "morning", "afternoon", "evening", "there", "peace", "greetings",
    "ji", "bot", "nusrah",
}

_MAX_RESULTS = 5


def _matches(word: str, tokens: List[str]) -> bool:
    """True if `word` is one of `tokens`, or (for longer words) a prefix of one of them,
    so 'masjid' matches 'masjids' but 'well' does not match 'wellness'."""
    if word in tokens:
        return True
    if len(word) >= 5:
        return any(t.startswith(word) for t in tokens)
    return False


def _detect_topics(tokens: List[str]) -> List[str]:
    found = []
    for topic, words in TOPICS.items():
        if any(_matches(w, tokens) for w in words):
            found.append(topic)
    return found


_ALL_TOPIC_WORDS = {w for words in TOPICS.values() for w in words}


def _extract_location(message_lower: str) -> Optional[str]:
    """Pulls a place name from phrases like 'in Hyderabad' or 'near Mumbai'."""
    m = re.search(r"\b(?:in|at|near|around|from)\s+([a-z]+(?:\s+[a-z]+){0,2})", message_lower)
    if not m:
        return None
    words = [
        w for w in m.group(1).split()
        if w not in _STOPWORDS and w not in _ALL_TOPIC_WORDS and not w.isdigit()
    ]
    return " ".join(words) if words else None


def _detect_mode(message_lower: str) -> Optional[str]:
    if re.search(r"\b(almost|nearly|close to|about to)\b.*\b(goal|funded|complete|completed|reach|reached)\b|\bnearly funded\b|\balmost funded\b", message_lower):
        return "nearly_funded"
    if re.search(r"\b(ending soon|closing soon|deadline|last days|expiring|expire|running out of time)\b", message_lower):
        return "ending_soon"
    return None


def _parse_end_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def find_campaigns(
    topics: List[str],
    location: Optional[str],
    mode: Optional[str] = None,
    limit: int = _MAX_RESULTS,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Returns (campaigns, exact_match). exact_match is False when we only found partial matches."""
    campaigns = list_active_campaigns()
    location_words = _tokens(location) if location else []
    required = len(topics) + (1 if location_words else 0)

    scored = []
    for c in campaigns:
        score = 0
        for t in topics:
            if any(_matches(w, c["_text_tokens"]) for w in TOPICS[t]):
                score += 1
        if location_words and all(_matches(w, c["_location_tokens"]) for w in location_words):
            score += 1
        scored.append((score, c))

    if required > 0:
        best = max((s for s, _ in scored), default=0)
        if best == 0:
            return [], False
        exact = best == required
        pool = [c for s, c in scored if s == best]
    else:
        exact = True
        pool = [c for _, c in scored]

    today = date.today()
    if mode == "nearly_funded":
        pool = [c for c in pool if 0 < c["progress"] < 100]
        pool.sort(key=lambda c: -c["progress"])
    elif mode == "ending_soon":
        dated = [(c, _parse_end_date(c["end_date"])) for c in pool]
        dated = [(c, d) for c, d in dated if d and d >= today]
        dated.sort(key=lambda cd: cd[1])
        pool = [c for c, _ in dated]
    else:
        # featured first, then campaigns that still need the most support
        pool.sort(key=lambda c: (not c["is_featured"], c["progress"]))

    return pool[:limit], exact


def search_campaigns(query: str, limit: int = _MAX_RESULTS) -> List[Dict[str, Any]]:
    """Free-text search used by the AI fallback as a tool. Returns public fields only."""
    tokens = _tokens(query)
    results, _ = find_campaigns(
        _detect_topics(tokens), _extract_location(query.lower()), _detect_mode(query.lower()), limit
    )
    return [_public_view(c) for c in results]


# --------------------------------------------------------------------------
# Site knowledge (also reused as context for the AI fallback)
# --------------------------------------------------------------------------

SITE_GUIDE = """Nusrah is a donation platform ("Aid that anchors you to Jannah") that connects donors directly with verified Masjids, orphanages, NGOs, schools, medical cases and community projects.
Pages: Home (/), Campaigns (/campaigns), Donate to a campaign (/donate/<campaign id>), Login (/login), Register (/register), About (/about), Contact (/contact).
Home page has a Zakah calculator (2.5% on cash, gold, silver, business assets and investments above the Nisab threshold, minus debts).
Donating: open a campaign, press Donate Now, choose an amount and pay online (Razorpay) or via a UPI QR code.
Starting a campaign: register or log in, submit the campaign form (title, location, category, goal, description, photos, verification documents, bank details). The Nusrah team reviews it; it goes live once approved and bank details are on file.
Volunteers help verify campaigns; volunteer accounts are approved by the admin team.
Every campaign is reviewed by the Nusrah team and volunteers before it is published, and organizers post progress updates."""


def _reply(
    text: str,
    campaigns: Optional[List[Dict[str, Any]]] = None,
    links: Optional[List[Dict[str, str]]] = None,
    suggestions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "reply": text,
        "campaigns": [_public_view(c) for c in (campaigns or [])],
        "links": links or [],
        "suggestions": suggestions or [],
        "source": "rules",
    }


_DEFAULT_SUGGESTIONS = [
    "Show Masjid campaigns",
    "How do I donate?",
    "How do I start a campaign?",
    "Calculate my zakah",
]

_PAGE_LINKS = {
    "campaigns": {"label": "Browse campaigns", "path": "/campaigns"},
    "about": {"label": "About Nusrah", "path": "/about"},
    "contact": {"label": "Contact us", "path": "/contact"},
    "login": {"label": "Log in", "path": "/login"},
    "register": {"label": "Create an account", "path": "/register"},
    "home": {"label": "Home page", "path": "/"},
}


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------

def _greeting(tokens: List[str]) -> Dict[str, Any]:
    is_salam = any(t in tokens for t in ("salam", "salaam", "assalamualaikum", "assalamu", "alaikum", "aslam"))
    opener = "Wa alaikum assalam!" if is_salam else "Hello!"
    return _reply(
        f"{opener} I'm the Nusrah assistant. I can help you find campaigns, explain how to donate or start a campaign, "
        "and take you to the right page. What would you like to do?",
        suggestions=_DEFAULT_SUGGESTIONS,
    )


def _capabilities() -> Dict[str, Any]:
    return _reply(
        "Here's what I can help with:\n"
        "• Find campaigns by type (Masjid, orphan care, medical, education...) or by city\n"
        "• Explain how to donate or start a campaign\n"
        "• Point you to the Zakah calculator\n"
        "• Take you to any page: campaigns, about, contact, login, register\n"
        "Just ask in your own words.",
        suggestions=_DEFAULT_SUGGESTIONS,
    )


def _about() -> Dict[str, Any]:
    return _reply(
        "Nusrah (\"Aid that anchors you to Jannah\") connects donors directly with verified Masjids, orphanages, NGOs, "
        "schools, medical cases and community projects. Every campaign is reviewed by our team and volunteers before it "
        "goes live, and organizers share progress updates so you can see the impact of your giving.",
        links=[_PAGE_LINKS["about"], _PAGE_LINKS["campaigns"]],
        suggestions=["How do I donate?", "Show active campaigns"],
    )


def _start_campaign() -> Dict[str, Any]:
    return _reply(
        "Starting a campaign takes a few steps:\n"
        "1. Register (or log in) with your account.\n"
        "2. Fill in the campaign form: title, location, category, goal amount, a clear description, photos, "
        "verification documents and the bank details where funds should go.\n"
        "3. Our team reviews your documents. Once approved, and with bank details on file, your campaign goes live.\n"
        "4. Donors give directly, and you post progress updates so they can see the impact.",
        links=[_PAGE_LINKS["register"], _PAGE_LINKS["login"]],
        suggestions=["How do I donate?", "Tell me about volunteers"],
    )


def _donate_how() -> Dict[str, Any]:
    return _reply(
        "Donating is simple:\n"
        "1. Open Campaigns and pick a cause you'd like to support.\n"
        "2. Press Donate Now on the campaign.\n"
        "3. Enter your amount and pay online, or scan the UPI QR code.\n"
        "Your donation is recorded on the campaign right away, so you can watch its progress grow.",
        links=[_PAGE_LINKS["campaigns"]],
        suggestions=["Show Masjid campaigns", "Show medical campaigns"],
    )


def _zakat() -> Dict[str, Any]:
    return _reply(
        "Our Zakah calculator is on the home page, in the \"Calculate Your Zakah\" section. Enter your cash, gold, silver, "
        "business assets, investments and any immediate debts. It works out 2.5% if your net wealth reaches the Nisab "
        "threshold.\n"
        "The calculator is a helpful guide. For rulings specific to your situation, please check with a qualified scholar.",
        links=[{"label": "Open the home page", "path": "/"}],
        suggestions=["Show active campaigns", "How do I donate?"],
    )


def _volunteer() -> Dict[str, Any]:
    return _reply(
        "Volunteers are the heart of Nusrah. They help verify campaigns so donors can give with confidence. "
        "To join, create an account through \"Join Our Team\" / Register. Volunteer accounts are approved by our admin "
        "team before they can start.",
        links=[_PAGE_LINKS["register"], _PAGE_LINKS["login"]],
        suggestions=["How do I start a campaign?", "How are campaigns verified?"],
    )


def _account() -> Dict[str, Any]:
    return _reply(
        "You can create an account on the Register page and sign in on the Login page. "
        "If you've forgotten your password, use the reset option on the Login page.",
        links=[_PAGE_LINKS["login"], _PAGE_LINKS["register"]],
    )


def _verification() -> Dict[str, Any]:
    return _reply(
        "Every campaign is submitted with verification documents and reviewed by the Nusrah team, with help from "
        "volunteers, before it goes live. Organizers also post progress updates, so you can see where your contribution goes.",
        links=[_PAGE_LINKS["campaigns"], _PAGE_LINKS["about"]],
        suggestions=["How do I donate?", "Show active campaigns"],
    )


def _contact() -> Dict[str, Any]:
    return _reply(
        "You can reach the Nusrah team through the Contact page. Send your message there and we'll get back to you.",
        links=[_PAGE_LINKS["contact"]],
    )


def _navigate(message_lower: str) -> Optional[Dict[str, Any]]:
    mapping = [
        (r"\babout\b", "about"),
        (r"\bcontact\b", "contact"),
        (r"\b(login|log in|sign in)\b", "login"),
        (r"\b(register|sign up|signup)\b", "register"),
        (r"\bcampaigns?\b", "campaigns"),
        (r"\bhome\b", "home"),
    ]
    for pattern, key in mapping:
        if re.search(pattern, message_lower):
            link = _PAGE_LINKS[key]
            return _reply(f"Sure, here's the link: {link['label']}.", links=[link])
    return None


def _count_campaigns() -> Dict[str, Any]:
    n = len(list_active_campaigns())
    return _reply(
        f"There {'is' if n == 1 else 'are'} {n} active campaign{'s' if n != 1 else ''} right now.",
        links=[_PAGE_LINKS["campaigns"]],
        suggestions=["Show active campaigns"],
    )


def _campaign_search(topics: List[str], location: Optional[str], mode: Optional[str]) -> Dict[str, Any]:
    label_parts = [TOPIC_LABELS[t] for t in topics]
    label = " & ".join(label_parts) if label_parts else "active"
    if location:
        label += f" campaigns in {location.title()}"
    else:
        label += " campaigns"
    if mode == "nearly_funded":
        label = "nearly funded " + label
    elif mode == "ending_soon":
        label = "ending-soon " + label

    try:
        results, exact = find_campaigns(topics, location, mode)
    except Exception as e:
        print(f"[chatbot_rules] campaign search failed: {e}")
        return _reply(
            "I couldn't load the campaigns just now. Please try again in a moment, or browse them directly.",
            links=[_PAGE_LINKS["campaigns"]],
        )

    browse = [_PAGE_LINKS["campaigns"]]
    if not results:
        return _reply(
            f"I couldn't find any {label} right now. You can browse all active campaigns instead.",
            links=browse,
            suggestions=["Show active campaigns", "Show Masjid campaigns"],
        )

    count = len(results)
    if exact:
        text = f"I found {count} {label}:" if count > 1 else f"I found 1 matching campaign ({label}):"
    else:
        text = f"I couldn't find an exact match for {label}, but these active campaigns are close:"
    return _reply(text, campaigns=results, links=browse, suggestions=["How do I donate?"])


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


def answer(message: str) -> Optional[Dict[str, Any]]:
    """Returns a reply dict, or None if the rules don't understand the question."""
    message = (message or "").strip()[:300]
    if not message:
        return _reply("Please type a question and I'll do my best to help.", suggestions=_DEFAULT_SUGGESTIONS)

    lower = message.lower()
    tokens = _tokens(lower)
    if not tokens:
        return None

    # Greetings / thanks / goodbyes
    if len(tokens) <= 5 and all(t in _GREETING_WORDS for t in tokens):
        return _greeting(tokens)
    if _has(lower, r"\b(thanks|thank you|thankyou|thx|jazakallah|jazak allah|shukran)\b"):
        return _reply("You're welcome! JazakAllah khair. Let me know if there's anything else I can help with.")
    if _has(lower, r"\b(bye|goodbye|see you|khuda hafiz|allah hafiz)\b"):
        return _reply("Take care! Come back any time you need help. Assalamu alaikum.")
    if _has(lower, r"\b(what can you do|what do you do|help me|menu|options|how can you help)\b") or lower in ("help", "?"):
        return _capabilities()

    # Informational questions
    if _has(lower, r"\b(forgot|forget|reset|lost)\b.*\bpassword\b|\bpassword\b.*\b(forgot|reset|lost)\b"):
        return _account()
    if _has(lower, r"\b(start|create|begin|launch|open|submit|raise|make|post)\b.*\b(campaign|fundrais\w*|appeal)\b|\bfundrais\w*\b"):
        return _start_campaign()
    if _has(lower, r"\bhow\b.*\b(donate|give|pay|contribute|donation)\b|\b(payment|pay)\b.*\b(method|methods|option|options|upi|card)\b"):
        return _donate_how()
    if _has(lower, r"\b(zakat|zakah|zakaat|nisab|nisaab)\b"):
        return _zakat()
    if _has(lower, r"\bvolunteer\w*\b|\bjoin (our|the) team\b"):
        return _volunteer()
    if _has(lower, r"\b(verify|verified|verification|trust|trusted|safe|genuine|legit|fake|scam|transparen\w+)\b"):
        return _verification()
    if _has(lower, r"\b(what is|about|who (are|is))\b.*\b(nusrah|this (app|site|website|platform))\b|\bwhat is this\b"):
        return _about()
    if _has(lower, r"\b(login|log in|sign in|signin|register|sign up|signup|create (an )?account)\b") and not _has(
        lower, r"\b(where|take me|go to|open|link)\b"
    ):
        return _account()
    if _has(lower, r"\b(contact|support|email|phone|reach (you|us)|helpdesk|help desk|complain\w*)\b"):
        return _contact()
    if _has(lower, r"\bhow many\b.*\bcampaigns?\b"):
        return _count_campaigns()
    if _has(lower, r"\b(where|take me|go to|open|navigate|link)\b"):
        nav = _navigate(lower)
        if nav:
            return nav

    # Campaign search
    topics = _detect_topics(tokens)
    location = _extract_location(lower)
    mode = _detect_mode(lower)
    asks_for_campaigns = any(t.startswith("campaign") for t in tokens) or any(t in _SEARCH_VERBS for t in tokens)
    if topics or mode or (location and asks_for_campaigns) or (
        asks_for_campaigns and any(t.startswith("campaign") for t in tokens)
    ):
        return _campaign_search(topics, location, mode)

    # Not understood → the AI fallback (or a friendly default) takes over
    return None