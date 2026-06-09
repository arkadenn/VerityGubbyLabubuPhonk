"""
Roblox "Brainrot" Game Detector + Discord Webhook Bot
------------------------------------------------------
- Polls Roblox game search API for games matching keywords (e.g. "Brainrot")
- Uses Google Gemini 1.5 Flash API to detect AI-generated thumbnails for FREE
- Sends flagged games to a Discord webhook with details + thumbnail
- Tracks already-seen game IDs to avoid duplicates

Requirements:
    pip install requests google-genai

Setup:
    1. Set GEMINI_API_KEY as an environment variable in your cloud host.
    2. Run: python roblox_brainrot_bot.py
"""

import os
import time
import json
import logging
import requests
from google import genai
from google.genai import types

# ─── CONFIG (PULLED SECURELY FROM THE CLOUD DASHBOARD OR CONFIG) ─────────────

# Your personal discord webhook pre-configured
DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL", 
    "https://discord.com/api/webhooks/1513904889879855266/XK_q913fLQFUTTzmmK8MAEcT7Z1F1AbvPU1drEb3kOqWT2UhzSnyATY7_2eH1oYve685"
)

SEARCH_KEYWORD        = "Brainrot"                  # keyword to search
POLL_INTERVAL_SECONDS = 120                         # how often to poll (seconds)
MAX_RESULTS_PER_POLL  = 20                          # games to check per poll

# On cloud servers, keeping seen IDs in memory prevents duplicate alerts during the runtime session.
# (If your host restarts, it will fetch current games, but won't duplicate unless they reappear in search)
seen_game_ids = set()

# Discord embed color (decimal): red-orange for "flagged"
EMBED_COLOR_AI        = 0xFF4500   # AI-generated thumbnail detected
EMBED_COLOR_NORMAL    = 0x5865F2   # matched keyword, no AI flag

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── ROBLOX API ───────────────────────────────────────────────────────────────

ROBLOX_SEARCH_URL    = "https://games.roblox.com/v1/games/list"
ROBLOX_THUMBNAIL_URL = "https://thumbnails.roblox.com/v1/games/multiget/thumbnails"

def search_roblox_games(keyword: str, limit: int = 20) -> list[dict]:
    """Search Roblox for games matching keyword. Returns list of game dicts."""
    params = {
        "model.keyword": keyword,
        "model.startRows": 0,
        "model.maxRows": limit,
        "model.sortToken": "",
        "model.gameFilter": "0",  # 0 = all
    }
    try:
        r = requests.get(ROBLOX_SEARCH_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("games", [])
    except Exception as e:
        log.warning(f"Roblox search error for '{keyword}': {e}")
        return []

def get_game_thumbnail_url(universe_id: int) -> str | None:
    """Fetch the thumbnail image URL for a game by universeId."""
    params = {
        "universeIds": universe_id,
        "countPerUniverse": 1,
        "defaults": "true",
        "size": "768x432",
        "format": "Png",
        "isCircular": "false",
    }
    try:
        r = requests.get(ROBLOX_THUMBNAIL_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        thumbnails = data.get("data", [])
        if thumbnails:
            images = thumbnails[0].get("thumbnails", [])
            if images:
                return images[0].get("imageUrl")
    except Exception as e:
        log.warning(f"Thumbnail fetch error for {universe_id}: {e}")
    return None

# ─── GEMINI VISION (FREE ALTERNATIVE) ────────────────────────────────────────

# Automatically initializes using the GEMINI_API_KEY environment variable
gemini_client = genai.Client()

AI_DETECTION_PROMPT = """You are an expert at detecting AI-generated images.

Look at this Roblox game thumbnail carefully. Decide whether it appears to be AI-generated
(e.g. made with Midjourney, Stable Diffusion, DALL-E, or similar tools).

Signs of AI generation include:
- Hyper-realistic or painterly style inconsistent with traditional Roblox art
- Odd anatomy, extra fingers, morphed text, or dreamlike distortions
- Suspiciously polished or "rendered" look not matching hand-made game art
- Watermarks or artifacts typical of AI tools

Respond ONLY with this JSON structure:
{
  "is_ai_generated": true or false,
  "confidence": "low" | "medium" | "high",
  "reason": "one short sentence"
}"""

def is_thumbnail_ai_generated(image_url: str) -> dict:
    """
    Downloads the thumbnail and asks Gemini 1.5 Flash whether it's AI-generated.
    """
    default = {"is_ai_generated": False, "confidence": "low", "reason": "Could not analyze"}
    try:
        img_resp = requests.get(image_url, timeout=10)
        img_resp.raise_for_status()
        image_bytes = img_resp.content
        mime_type = img_resp.headers.get("Content-Type", "image/png").split(";")[0]

        # Call Gemini 1.5 Flash (Generous Free Tier)
        response = gemini_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                AI_DETECTION_PROMPT
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",  # Forces Gemini to output pure JSON
                temperature=0.2
            )
        )
        
        result = json.loads(response.text.strip())
        return result
    except Exception as e:
        log.warning(f"Gemini Vision error: {e}")
        return default

# ─── DISCORD ──────────────────────────────────────────────────────────────────

def send_discord_alert(game: dict, thumbnail_url: str | None, ai_result: dict):
    """Post a Discord embed with game details and AI detection result."""
    universe_id   = game.get("universeId") or game.get("id")
    name          = game.get("name", "Unknown")
    description   = (game.get("description") or "No description")[:200]
    playing_count = game.get("playerCount", 0)
    creator       = game.get("creatorName") or (game.get("creator") or {}).get("name", "Unknown")
    game_url      = f"https://www.roblox.com/games/{universe_id}"

    is_ai    = ai_result.get("is_ai_generated", False)
    conf     = ai_result.get("confidence", "low")
    reason   = ai_result.get("reason", "")

    color    = EMBED_COLOR_AI if is_ai else EMBED_COLOR_NORMAL
    ai_label = f"⚠️ AI-Generated ({conf} confidence)" if is_ai else "✅ Likely human-made"

    embed = {
        "title": f"🎮 {name}",
        "url": game_url,
        "description": description,
        "color": color,
        "fields": [
            {"name": "Creator",      "value": creator,              "inline": True},
            {"name": "Players Now",  "value": str(playing_count),   "inline": True},
            {"name": "Thumbnail",    "value": ai_label,             "inline": False},
        ],
        "footer": {"text": "Roblox Brainrot Detector"},
    }

    if reason:
        embed["fields"].append({"name": "AI Analysis", "value": reason, "inline": False})

    if thumbnail_url:
        embed["image"] = {"url": thumbnail_url}

    payload = {
        "username": "Brainrot Watcher",
        "avatar_url": "https://www.roblox.com/favicon.ico",
        "embeds": [embed],
        "components": [
            {
                "type": 1,  # Action Row
                "components": [
                    {
                        "type": 2,        # Button
                        "style": 5,       # Link button
                        "label": "🎮 Open Game on Roblox",
                        "url": game_url,
                    }
                ],
            }
        ],
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 204:
            log.info(f"  ✔ Discord alert sent for: {name}")
        else:
            log.warning(f"  Discord webhook returned {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"  Discord send error: {e}")

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def process_game(game: dict) -> bool:
    """Process a single game result."""
    universe_id = game.get("universeId") or game.get("id")
    if not universe_id or universe_id in seen_game_ids:
        return False

    name = game.get("name", "?")
    log.info(f"  New game found: [{universe_id}] {name}")

    # Fetch thumbnail
    thumbnail_url = get_game_thumbnail_url(universe_id)

    # Run AI detection if we have a thumbnail
    if thumbnail_url:
        log.info(f"    Analyzing thumbnail with Gemini 1.5 Flash...")
        ai_result = is_thumbnail_ai_generated(thumbnail_url)
        flag = "⚠️  AI" if ai_result["is_ai_generated"] else "✅ Human"
        log.info(f"    Thumbnail: {flag} (confidence={ai_result['confidence']})")
    else:
        ai_result = {"is_ai_generated": False, "confidence": "low", "reason": "No thumbnail available"}

    # Send to Discord
    send_discord_alert(game, thumbnail_url, ai_result)

    seen_game_ids.add(universe_id)
    return True


def run():
    log.info("=" * 60)
    log.info("  Roblox Brainrot Detector — starting up")
    log.info(f"  Keyword:       {SEARCH_KEYWORD}")
    log.info(f"  Poll interval: {POLL_INTERVAL_SECONDS}s")
    log.info("=" * 60)

    # Check for the Google SDK env variable
    if not os.getenv("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY environment variable is missing! Set it before running.")
        return

    while True:
        log.info(f"--- Polling Roblox for '{SEARCH_KEYWORD}' ---")
        new_count = 0

        games = search_roblox_games(SEARCH_KEYWORD, limit=MAX_RESULTS_PER_POLL)
        log.info(f"  Found {len(games)} result(s).")

        for game in games:
            if process_game(game):
                new_count += 1
                time.sleep(1)  # small delay between API calls

        log.info(f"  Done. {new_count} new game(s) this poll. Sleeping {POLL_INTERVAL_SECONDS}s...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
