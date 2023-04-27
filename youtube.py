# -*- coding: utf-8 -*-

"""
MIT License

Copyright (c) 2022-Present Klappstuhl

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
OR OTHER DEALINGS IN THE SOFTWARE.
"""
from __future__ import annotations
import abc
import logging
import random
from pathlib import Path
from typing import List, Dict, Any, NamedTuple, Optional

import aiohttp
import discord
import json

from discord.ext import commands, tasks
import datetime

from discord.utils import cached_slot_property

logger = logging.getLogger(__name__)


class YouTubeRequestError(Exception):
    def __init__(self, reason: str, status: int):
        self.status = status
        self.reason = reason
        super().__init__(f"Request returned {status} thrown at: {reason}")


class config(abc.ABCMeta):
    """A class for getting and setting the config.json file.
    """

    path = Path(__file__).parent.parent / "config.json"

    @classmethod
    def get(cls) -> Dict[str, Any]:
        with open(cls.path, 'r', encoding='utf-8') as f:
            return json.load(f)


BASE_URL = "https://www.googleapis.com/youtube/v3/{endpoint}"
YOUTUBE_ICON_URL = "https://media.discordapp.net/attachments/1062074624935993427/1101142491199180831/youtube-icon.png?width=519&height=519"
YOUTUBE_VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"


class YouTubeChannel(NamedTuple):
    id: str
    name: str
    icon_url: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/channel/{self.id}"


class YouTubeStream(NamedTuple):
    channel: YouTubeChannel
    video_id: str
    started_at: datetime.datetime
    title: str
    description: str
    thumbnail_url: str

    @property
    def url(self) -> str:
        return YOUTUBE_VIDEO_URL.format(video_id=self.video_id)


class YouTubeNotifications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

        self.running_streams: List[YouTubeStream] = []

    async def cog_load(self) -> None:
        self.refresh_notify_check.start()

    async def cog_unload(self) -> None:
        if not self.session.closed:
            await self.session.close()
        self.refresh_notify_check.cancel()

    @cached_slot_property(name="_cs_api_key")
    def api_key(self) -> str:
        return config.get()["youtube"].get("api_key", None)

    @cached_slot_property(name="_cs_bearer_headers")
    def bearer_headers(self) -> dict:
        return {'Accept': 'application/json'}

    def payload(self, **params: Any) -> Dict[str, Any]:
        payload = {"key": self.api_key, **params}
        return payload

    async def get_channels(self, channel_names: List[str]) -> Optional[List[YouTubeChannel]]:
        cache = []

        for name in channel_names:
            payload = self.payload(forUsername=name, part="id,snippet")
            async with self.session.get(BASE_URL.format(endpoint="channels"), params=payload,
                                        headers=self.bearer_headers) as resp:
                if resp.status != 200:
                    raise YouTubeRequestError(f'Could not get channel "{name}".', resp.status)

                data = await resp.json()

                if not data.get("items", None):
                    continue

                channel = data["items"][0]
                cache.append(
                    YouTubeChannel(
                        id=channel["id"],
                        name=channel["snippet"]["title"],
                        icon_url=channel["snippet"]["thumbnails"]["default"]["url"]
                    )
                )

        return cache

    async def get_streams(self, channels: List[YouTubeChannel]):
        cache = []

        for channel in channels:
            payload = self.payload(part="snippet", channelId=channel.id, type="video", eventType="live",
                                   maxResults=1, order="date")
            async with self.session.get(BASE_URL.format(endpoint="search"), params=payload,
                                        headers=self.bearer_headers) as resp:
                if resp.status != 200:
                    raise YouTubeRequestError(f'Could not get stream for channel "{channel.id}".', resp.status)

                data = await resp.json()

                if not data.get("items", None):
                    continue

                stream = data["items"][0]
                cache.append(
                    YouTubeStream(
                        channel=channel,
                        video_id=stream["id"]["videoId"],
                        started_at=datetime.datetime.fromisoformat(stream["snippet"]["publishedAt"])
                        .astimezone(datetime.timezone.utc),
                        title=stream["snippet"]["title"],
                        description=stream["snippet"]["description"],
                        thumbnail_url=stream["snippet"]["thumbnails"]["default"]["url"]
                    )
                )

        return cache

    async def get_notifications(self) -> List[YouTubeStream]:
        wl = config.get()["youtube"]["watchlist"]
        streams = await self.get_streams(await self.get_channels(wl))

        cache = []
        
        for stream in self.running_streams:
            if stream not in streams:
                self.running_streams.remove(stream)
        
        for stream in streams:
            if stream in self.running_streams:
                continue

            cache.append(stream)
            self.running_streams.append(stream)

        return cache

    @tasks.loop(minutes=2)
    async def refresh_notify_check(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(config.get()["youtube"]["channel_id"])
        if not channel:
            raise discord.DiscordException("YouTube Notification channel not found.")

        streams = await self.get_notifications()
        for stream in streams:
            embed = discord.Embed(title=stream.title,
                                  description=stream.description,
                                  url=stream.url, color=random.randint(0, 0xFFFFFF))
            embed.set_author(name=f"{stream.channel.name} ist jetzt Live auf YouTube!", url=stream.channel.url,
                             icon_url=YOUTUBE_ICON_URL)
            embed.set_thumbnail(url=stream.channel.icon_url)
            embed.add_field(name="Gestartet", value=discord.utils.format_dt(stream.started_at, style="R"),
                            inline=False)
            embed.set_image(url=stream.thumbnail_url)

            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                logger.warning("Could not send twitch notification due to: %s", exc_info=True)


async def setup(bot):
    await bot.add_cog(YouTubeNotifications(bot))
