"""Live-data tools Krishna can call mid-conversation -- the actual fix for
"my knowledge cutoff is December 2023": no local LLM can know anything
after its training cutoff no matter how it's prompted, so answering
anything time-sensitive means fetching it live and handing the real
answer to the model as context, not asking the model to recall it.

Each tool is a plain Python function with a Google-style docstring --
Ollama's client auto-converts that into a tool schema (verified: llama3.2:3b
correctly identifies when to call get_weather and extracts the right
argument, tested live before this was wired into the main app).

All tools here are read-only public lookups, no API key, no personal data
-- the safe category, same reasoning as the notes feature. News needs a
Currents API key (not yet provided) so it's a placeholder for now.
"""
from __future__ import annotations

import requests


def get_weather(location: str) -> str:
    """Get the current weather for a location.

    Args:
        location: City or place name, e.g. "New Jersey" or "Mumbai"
    """
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1}, timeout=10).json()
        if not geo.get("results"):
            return f"Could not find a location matching '{location}'."
        r = geo["results"][0]
        lat, lon, name = r["latitude"], r["longitude"], r["name"]
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current_weather": "true"},
            timeout=10).json()
        cw = w["current_weather"]
        return (f"Current weather in {name}: {cw['temperature']}°C, "
               f"wind {cw['windspeed']} km/h.")
    except Exception as e:
        return f"Weather lookup failed: {e}"


def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert an amount from one currency to another using current exchange rates.

    Args:
        amount: The amount to convert, e.g. 100
        from_currency: Three-letter currency code, e.g. "USD"
        to_currency: Three-letter currency code, e.g. "INR"
    """
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest",
            params={"amount": amount, "from": from_currency.upper(),
                    "to": to_currency.upper()}, timeout=10).json()
        rate = r["rates"].get(to_currency.upper())
        if rate is None:
            return f"Could not find a rate for {to_currency}."
        return f"{amount} {from_currency.upper()} = {rate} {to_currency.upper()}"
    except Exception as e:
        return f"Currency conversion failed: {e}"


def define_word(word: str) -> str:
    """Look up the dictionary definition of a word.

    Args:
        word: The word to define
    """
    try:
        r = requests.get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=10)
        if r.status_code != 200:
            return f"No definition found for '{word}'."
        data = r.json()[0]
        meaning = data["meanings"][0]
        pos = meaning["partOfSpeech"]
        definition = meaning["definitions"][0]["definition"]
        return f"{word} ({pos}): {definition}"
    except Exception as e:
        return f"Dictionary lookup failed: {e}"


def tell_joke() -> str:
    """Tell a random joke."""
    try:
        r = requests.get("https://v2.jokeapi.dev/joke/Any?safe-mode",
                         timeout=10).json()
        if r.get("type") == "single":
            return r["joke"]
        return f"{r['setup']} ... {r['delivery']}"
    except Exception as e:
        return f"Joke lookup failed: {e}"


# News needs a free Currents API key (not yet provided) -- listed here as a
# placeholder so it's visible in ALL_TOOLS rather than silently missing, but
# not registered as an active tool until a key exists.
ALL_TOOLS = [get_weather, convert_currency, define_word, tell_joke]
