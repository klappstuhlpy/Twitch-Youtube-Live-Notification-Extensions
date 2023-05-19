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
import abc
import logging
import random
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, NamedTuple

import aiohttp
import discord
import json

from discord import HTTPException
from discord.ext import commands, tasks
import datetime

from discord.utils import cached_slot_property

logger = logging.getLogger(__name__)


class TwitchRequestError(HTTPException):
    """A subclass Exception for failed Twitch API requests."""
    pass


GRANT_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_ICON_URL = "https://media.discordapp.net/attachments/1062074624935993427/1101142491450835036/5968819.png"


class config:
    """A class for getting and setting the config.json file."""

    path = Path(__file__).parent.parent / "config.json"

    @classmethod
    def set(cls, **params: dict | Any) -> None:
        payload = cls.get()

        with open(cls.path, "w") as file:
            payload["twitch"].update(params)
            json.dump(payload, file, indent=4)

    @classmethod
    def get(cls) -> Dict[str, Any]:
        with open(cls.path, 'r', encoding='utf-8') as f:
            return json.load(f)


class TwitchUser(NamedTuple):
    id: str
    login: str
    display_name: str
    type: str
    broadcaster_type: str
    description: str
    profile_image_url: str
    offline_image_url: str
    view_count: int

    @property
    def url(self) -> str:
        return f"https://twitch.tv/{self.login}"


class TwitchStream(NamedTuple):
    id: str
    user: TwitchUser
    game_id: str
    game_name: str
    type: str
    title: str
    tags: List[str]
    viewer_count: int
    started_at: str
    language: str
    thumbnail_url: str


class TwitchNotifications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

        self.online_users: List[str] = []

    async def cog_load(self) -> None:
        self.refresh_notify_check.start()

    async def cog_unload(self) -> None:
        if not self.session.closed:
            await self.session.close()
        self.refresh_notify_check.cancel()

    def _expiry(self, expiry: Optional[float] = None) -> float:
        if expiry:  # Set expiry time
            config.set(expiry=expiry)
        return config.get()["twitch"].get("expiry", None)

    def _bearer_token(self, bearer_token: Optional[str] = None) -> str:
        if not self._expiry() or (self._expiry() < time.time()):
            logger.debug("Refreshing bearer token")
            self.bot.loop.create_task(self._get_bearer_token())

        if bearer_token:
            config.set(bearer_token=bearer_token)
        return config.get()["twitch"].get("bearer_token", None)

    @cached_slot_property(name="_cs_grant_params")
    def grant_params(self) -> dict:
        return {'client_id': config.get()["twitch"]["client_id"],
                'client_secret': config.get()["twitch"]["client_secret"],
                'grant_type': 'client_credentials',
                'Content-Type': 'application/x-www-form-urlencoded'}

    @cached_slot_property(name="_cs_bearer_headers")
    def bearer_headers(self) -> dict:
        return {'Authorization': f'Bearer {self._bearer_token()}',
                'Client-Id': config.get()["twitch"]["client_id"]}

    async def _get_bearer_token(self) -> None:
        async with self.session.post(GRANT_URL, params=self.grant_params) as resp:
            if resp.status != 200:
                raise TwitchRequestError(resp, resp.reason)

            data = await resp.json()
            self._expiry(expiry=(time.time() + (int(data['expires_in']) - 10)))
            self._bearer_token(bearer_token=data['access_token'])

    async def get_users(self, login_names: List[str]) -> List[TwitchUser]:
        payload = {"login": login_names}

        async with self.session.get("https://api.twitch.tv/helix/users", params=payload,
                                    headers=self.bearer_headers) as resp:
            if resp.status != 200:
                raise TwitchRequestError(resp, "Could not get user IDs. Maybe refresh the bearer token?")

            data = await resp.json()
            return [TwitchUser(
                id=entry["id"],
                login=entry["login"],
                display_name=entry["display_name"],
                type=entry["type"],
                broadcaster_type=entry["broadcaster_type"],
                description=entry["description"],
                profile_image_url=entry["profile_image_url"],
                offline_image_url=entry["offline_image_url"],
                view_count=entry["view_count"]
            ) for entry in data["data"] if entry["login"] in login_names]

    async def get_streams(self, users: List[TwitchUser]) -> List[TwitchStream]:
        payload = {"user_id": [user.id for user in users]}

        async with self.session.get("https://api.twitch.tv/helix/streams", params=payload,
                                    headers=self.bearer_headers) as resp:
            if resp.status != 200:
                raise TwitchRequestError(resp, "Could not get streams")

            data = await resp.json()
            return [
                TwitchStream(
                    id=entry["id"],
                    user=discord.utils.get(users, id=entry["user_id"]),
                    game_id=entry["game_id"],
                    game_name=entry["game_name"],
                    type=entry["type"],
                    title=entry["title"],
                    tags=entry["tags"],
                    viewer_count=entry["viewer_count"],
                    started_at=entry["started_at"],
                    language=entry["language"],
                    thumbnail_url=entry["thumbnail_url"]
                ) for entry in data["data"]
            ]

    async def get_notifications(self) -> Optional[List[TwitchStream]]:
        wl = config.get()["twitch"]["watchlist"]
        users = await self.get_users(wl)
        streams = await self.get_streams(users)

        cache = []
        for user_name in wl:
            stream = discord.utils.get(streams, user__login=user_name)
            if not stream:
                try:
                    self.online_users.remove(user_name)
                except ValueError:
                    pass
                finally:
                    continue

            if user_name in self.online_users:
                continue

            cache.append(stream)
            self.online_users.append(user_name)

        return cache

    @tasks.loop(minutes=2)
    async def refresh_notify_check(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(config.get()["twitch"]["channel_id"])
        if not channel:
            raise discord.DiscordException("Twitch Notification channel not found.")

        streams = await self.get_notifications()
        for stream in streams:
            started_at = datetime.datetime.fromisoformat(stream.started_at).astimezone(datetime.timezone.utc)

            embed = discord.Embed(title=stream.title, url=stream.user.url, color=0x6441a5)
            embed.set_author(name=f"{stream.user.display_name} is now Live on Twitch!", url=stream.user.url,
                             icon_url=TWITCH_ICON_URL)
            embed.set_thumbnail(url=stream.user.profile_image_url)
            embed.add_field(name="Started", value=discord.utils.format_dt(started_at, style="R"),
                            inline=False)
            embed.add_field(name="Game", value=stream.game_name or 'Unknown', inline=True)
            embed.add_field(name="Viewers", value=f"{stream.viewer_count:,}", inline=True)
            if tags := stream.tags:
                embed.add_field(name="Tags", value=", ".join(tags), inline=False)
            embed.set_image(url=stream.thumbnail_url.format(width=1920, height=1080))

            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                logger.warning("Could not send twitch notification due to: %s", exc_info=True)


async def setup(bot):
    await bot.add_cog(TwitchNotifications(bot))
