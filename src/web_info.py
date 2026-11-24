"""Weather and news for Jarvis, from free sources that need no API key.

Weather: Open-Meteo (geocoding + forecast). News: Google News RSS feeds.
"""

from __future__ import annotations

import random
import re
import socket
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import aiohttp
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

DEFAULT_LOCATION = "Lorena, São Paulo, Brazil"
TIMEOUT = aiohttp.ClientTimeout(total=12)
NEWS_FEEDS = {
    "brazil": "https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "world": "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en",
}
NEWS_SEARCH = "https://news.google.com/rss/search"

# WMO weather interpretation codes used by Open-Meteo.
WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "severe thunderstorms with hail",
}


class InfoError(Exception):
    pass


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict) -> Any:
    try:
        async with session.get(url, params=params, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise InfoError("The weather service did not answer.") from exc


async def geocode(session: aiohttp.ClientSession, place: str) -> dict[str, Any]:
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if not parts:
        raise InfoError("Tell me which place.")
    data = await _get_json(
        session,
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": parts[0], "count": 10, "language": "en", "format": "json"},
    )
    results = data.get("results") or []
    hints = [h.casefold() for h in parts[1:]]

    def score(result: dict[str, Any]) -> int:
        text = " ".join(
            str(result.get(k, "")) for k in ("admin1", "admin2", "country")
        ).casefold()
        return sum(hint in text for hint in hints)

    if not results:
        raise InfoError(f"I could not find a place called {parts[0]}.")
    best = max(results, key=score)
    name = ", ".join(str(best[k]) for k in ("name", "admin1", "country") if best.get(k))
    return {"lat": best["latitude"], "lon": best["longitude"], "name": name}


async def forecast(
    session: aiohttp.ClientSession, lat: float, lon: float
) -> dict[str, Any]:
    data = await _get_json(
        session,
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,weather_code",
            "timezone": "auto",
            "forecast_days": 2,
        },
    )
    current = data.get("current") or {}
    daily = data.get("daily") or {}

    def day(index: int) -> dict[str, Any]:
        return {
            "date": (daily.get("time") or [None, None])[index],
            "max_c": (daily.get("temperature_2m_max") or [None, None])[index],
            "min_c": (daily.get("temperature_2m_min") or [None, None])[index],
            "rain_chance_percent": (
                daily.get("precipitation_probability_max") or [None, None]
            )[index],
            "conditions": WEATHER_CODES.get(
                (daily.get("weather_code") or [None, None])[index], "unknown"
            ),
        }

    return {
        "now": {
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "wind_kmh": current.get("wind_speed_10m"),
            "conditions": WEATHER_CODES.get(current.get("weather_code"), "unknown"),
        },
        "today": day(0),
        "tomorrow": day(1),
    }


def parse_feed(xml: bytes, limit: int) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise InfoError("The news feed was unreadable.") from exc
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        # Google News appends " - Source" to titles.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        published = item.findtext("pubDate")
        try:
            when = (
                parsedate_to_datetime(published).strftime("%Y-%m-%d %H:%M")
                if published
                else None
            )
        except (TypeError, ValueError):
            when = None
        if title:
            items.append(
                {
                    "headline": re.sub(r"\s+", " ", title),
                    "source": source,
                    "published": when,
                }
            )
        if len(items) >= limit:
            break
    return items


class WebInfoTools:
    def __init__(
        self,
        default_place: str = DEFAULT_LOCATION,
        default_coords: dict[str, float] | None = None,
    ) -> None:
        self.default_place = default_place
        self.default_coords = default_coords
        self._session: aiohttp.ClientSession | None = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # IPv4 only: some home networks advertise IPv6 routes that time out.
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "JARVIS home assistant"},
                connector=aiohttp.TCPConnector(family=socket.AF_INET),
            )
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()

    @property
    def tools(self) -> list:
        return [self.get_weather, self.get_news]

    @function_tool()
    async def get_weather(self, context: RunContext, place: str = "") -> dict[str, Any]:
        """Current weather and today's and tomorrow's forecast, in Celsius.

        Leave place empty for where the user is now (their phone's location, or
        their home town). Name a city to check somewhere else.

        Args:
            place: A city, optionally with state and country, e.g. "Lorena, São
                Paulo, Brazil". Empty for the user's current location.
        """
        try:
            session = self._client()
            if place.strip():
                where = await geocode(session, place)
            elif self.default_coords:
                where = {**self.default_coords, "name": "the user's current location"}
            else:
                where = await geocode(session, self.default_place)
            report = await forecast(session, where["lat"], where["lon"])
        except InfoError as exc:
            raise ToolError(str(exc)) from exc
        return {"place": where["name"], **report}

    @function_tool()
    async def get_news(
        self, context: RunContext, topic: str = "", region: str = "brazil"
    ) -> dict[str, Any]:
        """Today's news headlines from Google News, in a random order.

        Use it for news, current events, or a random story for the user. Headlines
        may be in Portuguese; translate and summarise them in English.

        Args:
            topic: Optional subject to search for, e.g. "technology". Empty for top
                stories.
            region: "brazil" for Brazilian news or "world" for international news.
        """
        if topic.strip():
            url = NEWS_SEARCH
            params = (
                {"q": topic.strip(), "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"}
                if region != "world"
                else {"q": topic.strip(), "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
            )
        else:
            url, params = NEWS_FEEDS.get(region, NEWS_FEEDS["brazil"]), {}
        try:
            async with self._client().get(url, params=params, timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                items = parse_feed(await resp.read(), limit=12)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ToolError("The news service did not answer.") from exc
        except InfoError as exc:
            raise ToolError(str(exc)) from exc
        random.shuffle(items)
        return {"headlines": items[:8], "count": len(items)}
