import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import discord
import requests
from discord.ext import commands

VERSION = "1.1.0"
CONFIG_PATH = Path("config.json")
GAME_CONFIG_PATH = Path("game.json")
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_RESET = "\033[0m"
BANNER = r"""
  /$$$$$$  /$$$$$$$$ /$$       /$$$$$$$$ /$$$$$$$   /$$$$$$  /$$$$$$$$
 /$$__  $$| $$_____/| $$      | $$_____/| $$__  $$ /$$__  $$|__  $$__/
| $$  \__/| $$      | $$      | $$      | $$  \ $$| $$  \ $$   | $$
|  $$$$$$ | $$$$$   | $$      | $$$$$   | $$$$$$$ | $$  | $$   | $$
 \____  $$| $$__/   | $$      | $$__/   | $$__  $$| $$  | $$   | $$
 /$$  \ $$| $$      | $$      | $$      | $$  \ $$| $$  | $$   | $$
|  $$$$$$/| $$$$$$$$| $$$$$$$$| $$      | $$$$$$$/|  $$$$$$/   | $$
 \______/ |________/|________/|__/      |_______/  \______/    |__/
"""

logging.basicConfig(level=logging.CRITICAL)
logging.getLogger("discord").setLevel(logging.CRITICAL)
logging.getLogger("discord.http").setLevel(logging.CRITICAL)
logging.getLogger("discord.gateway").setLevel(logging.CRITICAL)
logging.getLogger("discord.client").setLevel(logging.CRITICAL)


def ok(message: str) -> str:
    return f"{COLOR_GREEN}{message}{COLOR_RESET}"


def err(message: str) -> str:
    return f"{COLOR_RED}{message}{COLOR_RESET}"


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Файл конфига не найден: {path}")
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def normalize_status_type(value: str) -> str:
    v = str(value).lower().strip()
    if v == "stream":
        return "streaming"
    return v


def build_playing_activity(game_cfg: Dict[str, Any], name: str) -> discord.BaseActivity:
    game_block = game_cfg.get("game", {})
    application_id = game_block.get("application_id")
    if application_id:
        return discord.Activity(
            type=discord.ActivityType.playing,
            name=name,
            application_id=int(application_id),
        )
    return discord.Game(name=name)


def build_streaming_activity(game_cfg: Dict[str, Any], name: str) -> discord.Activity:
    stream_cfg = game_cfg.get("stream", {})
    stream_url = str(stream_cfg.get("url", "")).strip()
    if not stream_url:
        raise ValueError("game.json: stream.url обязателен")
    activity_data: Dict[str, Any] = {
        "type": discord.ActivityType.streaming,
        "name": name,
        "url": stream_url,
    }

    application_id = stream_cfg.get("application_id")
    if application_id:
        activity_data["application_id"] = int(application_id)

    assets: Dict[str, Any] = {}
    image = stream_cfg.get("image")
    if image:
        assets["large_image"] = image

    if assets:
        activity_data["assets"] = assets

    return discord.Activity(**activity_data)


def build_activity(mode: str, text: str, game_cfg: Dict[str, Any]) -> discord.BaseActivity:
    mode = normalize_status_type(mode)
    if mode == "playing":
        return build_playing_activity(game_cfg=game_cfg, name=text)
    if mode == "streaming":
        return build_streaming_activity(game_cfg=game_cfg, name=text)
    raise ValueError(f"Неподдерживаемый режим статуса: {mode}")


class UserBot(commands.Bot):
    def __init__(self, config: Dict[str, Any], game_cfg: Dict[str, Any]):
        self.config = config
        self.game_cfg = game_cfg
        prefix = str(config.get("prefix", "."))
        super().__init__(command_prefix=prefix, self_bot=True, help_command=None)

        raw_owner_id = config.get("owner_id")
        self.owner_id: Optional[int] = int(raw_owner_id) if str(raw_owner_id).isdigit() and int(raw_owner_id) > 0 else None
        self.current_status_mode: str = "none"
        self.current_status_text: str = ""
        self.startup_banner_printed: bool = False
        self.rotation_error_reported: bool = False
        self.stream_keepalive_error_reported: bool = False

        self.rotation_task: Optional[asyncio.Task] = None
        self.stream_keepalive_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        await self.restart_rotation_task()
        await self.restart_stream_keepalive_task()

    async def close(self) -> None:
        if self.rotation_task:
            self.rotation_task.cancel()
        if self.stream_keepalive_task:
            self.stream_keepalive_task.cancel()
        try:
            await self.clear_custom_status()
        except Exception:
            pass
        await super().close()

    async def restart_rotation_task(self) -> None:
        if self.rotation_task and not self.rotation_task.done():
            self.rotation_task.cancel()
        self.rotation_task = None

        if self.config.get("status_rotation", {}).get("enabled", False):
            self.rotation_task = asyncio.create_task(self.status_rotation_loop())

    async def restart_stream_keepalive_task(self) -> None:
        if self.stream_keepalive_task and not self.stream_keepalive_task.done():
            self.stream_keepalive_task.cancel()
        self.stream_keepalive_task = None

        keepalive_cfg = self.config.get("stream_keepalive", {})
        if bool(keepalive_cfg.get("enabled", True)):
            self.stream_keepalive_task = asyncio.create_task(self.stream_keepalive_loop())

    async def apply_status(self, mode: str, text: str) -> None:
        activity = build_activity(mode=mode, text=text, game_cfg=self.game_cfg)
        await self.change_presence(activity=activity)
        self.current_status_mode = normalize_status_type(mode)
        self.current_status_text = text

    def get_startup_mode_text(self) -> Tuple[Optional[str], Optional[str]]:
        startup_cfg = self.game_cfg.get("startup", {})
        game_on = bool(startup_cfg.get("game", False))
        stream_on = bool(startup_cfg.get("stream", False))

        game_text = str(self.game_cfg.get("game", {}).get("name", "")).strip()
        stream_text = str(self.game_cfg.get("stream", {}).get("name", "")).strip()

        if game_on and stream_on:
            return "streaming", stream_text
        if stream_on:
            return "streaming", stream_text
        if game_on:
            return "playing", game_text
        return None, None

    def get_status_name_from_config(self, mode: str) -> str:
        mode = normalize_status_type(mode)
        if mode == "playing":
            return str(self.game_cfg.get("game", {}).get("name", "")).strip()
        if mode == "streaming":
            return str(self.game_cfg.get("stream", {}).get("name", "")).strip()
        return ""

    def validate_mode_config(self, mode: str) -> Optional[str]:
        mode = normalize_status_type(mode)
        if mode == "playing":
            game_block = self.game_cfg.get("game", {})
            if not str(game_block.get("name", "")).strip():
                return "В game.json не заполнено game.name"
            if not game_block.get("application_id"):
                return "В game.json не заполнено game.application_id"
            return None

        if mode == "streaming":
            stream_block = self.game_cfg.get("stream", {})
            if not str(stream_block.get("name", "")).strip():
                return "В game.json не заполнено stream.name"
            if not stream_block.get("application_id"):
                return "В game.json не заполнено stream.application_id"
            if not str(stream_block.get("url", "")).strip():
                return "В game.json не заполнено stream.url"
            return None

        return "Неизвестный режим статуса"

    async def apply_startup_status(self) -> None:
        mode, text = self.get_startup_mode_text()
        if mode is None:
            return
        config_error = self.validate_mode_config(mode)
        if config_error:
            return
        await self.apply_status(mode=mode, text=text)

    async def status_rotation_loop(self) -> None:
        await self.wait_until_ready()
        cfg = self.config.get("status_rotation", {})

        texts = cfg.get("texts", [])
        interval = int(cfg.get("interval_seconds", 60))

        if not texts:
            return
        index = 0

        while not self.is_closed():
            try:
                text = str(texts[index % len(texts)])
                await self.set_custom_status(text)
                index += 1
                self.rotation_error_reported = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self.rotation_error_reported:
                    print(err(f"Ошибка ротации статуса: {exc}. Решение: проверь token и настройки status_rotation/custom_emoji."))
                    self.rotation_error_reported = True

            await asyncio.sleep(interval)

    async def stream_keepalive_loop(self) -> None:
        await self.wait_until_ready()
        keepalive_cfg = self.config.get("stream_keepalive", {})
        interval = int(keepalive_cfg.get("interval_seconds", 180))

        while not self.is_closed():
            try:
                if self.current_status_mode == "streaming":
                    text = self.current_status_text or self.get_status_name_from_config("streaming")
                    if text:
                        await self.apply_status("streaming", text)
                self.stream_keepalive_error_reported = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self.stream_keepalive_error_reported:
                    print(err(f"Ошибка keepalive стрима: {exc}. Решение: проверь stream настройки в game.json."))
                    self.stream_keepalive_error_reported = True

            await asyncio.sleep(interval)

    async def set_custom_status(self, text: str) -> None:
        token = str(self.config.get("token", "")).strip()
        if not token:
            raise ValueError("config.json: token обязателен для ротации кастомного статуса")

        payload: Dict[str, Any] = {"custom_status": {"text": text}}
        rotation_cfg = self.config.get("status_rotation", {})
        emoji_cfg = rotation_cfg.get("custom_emoji", {})
        if isinstance(emoji_cfg, dict) and bool(emoji_cfg.get("enabled", False)):
            emoji_name = str(emoji_cfg.get("emoji_name", "")).strip()
            emoji_id = str(emoji_cfg.get("emoji_id", "")).strip()
            if not emoji_name:
                raise ValueError("status_rotation.custom_emoji.emoji_name пустой")

            payload["custom_status"]["emoji_name"] = emoji_name
            if emoji_id:
                payload["custom_status"]["emoji_id"] = emoji_id

        headers = {
            "authorization": token,
            "content-type": "application/json",
        }

        def _patch() -> requests.Response:
            return requests.patch(
                "https://discord.com/api/v9/users/@me/settings",
                headers=headers,
                json=payload,
                timeout=15,
            )

        response = await asyncio.to_thread(_patch)
        response.raise_for_status()

    async def clear_custom_status(self) -> None:
        token = str(self.config.get("token", "")).strip()
        if not token:
            return

        headers = {
            "authorization": token,
            "content-type": "application/json",
        }

        def _patch() -> requests.Response:
            return requests.patch(
                "https://discord.com/api/v9/users/@me/settings",
                headers=headers,
                json={"custom_status": None},
                timeout=15,
            )

        response = await asyncio.to_thread(_patch)
        response.raise_for_status()

    async def call_ai(self, prompt: str) -> str:
        ai_cfg = self.config.get("ai", {})
        endpoint = ai_cfg.get("endpoint", "https://api.onlysq.ru/ai/v2")
        api_key = ai_cfg.get("api_key", "openai")
        model = ai_cfg.get("model", "gpt-4o-mini")
        timeout = int(ai_cfg.get("timeout_seconds", 40))
        system_prompt = str(ai_cfg.get("system_prompt", "")).strip()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "request": {
                "messages": messages
            }
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        def _request() -> requests.Response:
            return requests.post(endpoint, headers=headers, json=payload, timeout=timeout)

        response = await asyncio.to_thread(_request)
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("OnlySQ returned no choices")

        message = choices[0].get("message", {})
        content = message.get("content")
        if not content:
            raise RuntimeError("OnlySQ returned empty content")

        max_chars = int(ai_cfg.get("max_reply_chars", 1800))
        return str(content)[:max_chars]


def ensure_config_values(config: Dict[str, Any], game_cfg: Dict[str, Any]) -> None:
    if not config.get("token"):
        raise ValueError("config.json: поле token обязательно")

    status_rotation = config.get("status_rotation", {})
    if status_rotation.get("enabled", False):
        interval = int(status_rotation.get("interval_seconds", 60))
        if interval < 5:
            raise ValueError("config.json: status_rotation.interval_seconds должен быть >= 5")
        texts = status_rotation.get("texts", [])
        if not isinstance(texts, list) or len(texts) == 0:
            raise ValueError("config.json: status_rotation.texts должен быть непустым массивом")

        emoji_cfg = status_rotation.get("custom_emoji", {})
        if isinstance(emoji_cfg, dict) and bool(emoji_cfg.get("enabled", False)):
            if not str(emoji_cfg.get("emoji_name", "")).strip():
                raise ValueError("config.json: status_rotation.custom_emoji.emoji_name обязателен при enabled=true")

    keepalive_cfg = config.get("stream_keepalive", {})
    if bool(keepalive_cfg.get("enabled", True)):
        interval = int(keepalive_cfg.get("interval_seconds", 180))
        if interval < 30:
            raise ValueError("config.json: stream_keepalive.interval_seconds должен быть >= 30")

    if not isinstance(game_cfg, dict):
        raise ValueError("game.json: неверный формат")

    startup_cfg = game_cfg.get("startup", {})
    if not isinstance(startup_cfg, dict):
        raise ValueError("game.json: startup должен быть объектом")
    if "game" not in startup_cfg or "stream" not in startup_cfg:
        raise ValueError("game.json: startup должен содержать флаги game и stream")

    game_block = game_cfg.get("game", {})
    stream_block = game_cfg.get("stream", {})
    if not isinstance(game_block, dict) or not isinstance(stream_block, dict):
        raise ValueError("game.json: блоки game и stream должны быть объектами")

    if bool(startup_cfg.get("game", False)) and not game_block.get("application_id"):
        raise ValueError("game.json: game.application_id обязателен при startup.game=true")
    if bool(startup_cfg.get("game", False)) and not str(game_block.get("name", "")).strip():
        raise ValueError("game.json: game.name обязателен при startup.game=true")

    if bool(startup_cfg.get("stream", False)) and not stream_block.get("application_id"):
        raise ValueError("game.json: stream.application_id обязателен при startup.stream=true")
    if bool(startup_cfg.get("stream", False)) and not str(stream_block.get("url", "")).strip():
        raise ValueError("game.json: stream.url обязателен при startup.stream=true")
    if bool(startup_cfg.get("stream", False)) and not str(stream_block.get("name", "")).strip():
        raise ValueError("game.json: stream.name обязателен при startup.stream=true")


config = load_json(CONFIG_PATH)
game_config = load_json(GAME_CONFIG_PATH)
ensure_config_values(config, game_config)

bot = UserBot(config=config, game_cfg=game_config)


@bot.event
async def on_ready() -> None:
    if bot.owner_id is None and bot.user:
        bot.owner_id = bot.user.id

    try:
        await bot.apply_startup_status()
    except Exception as exc:
        print(err(f"Ошибка запуска: {exc}. Решение: проверь game.json и .reloadcfg."))

    if not bot.startup_banner_printed:
        mode, text = bot.get_startup_mode_text()
        config_error = bot.validate_mode_config(mode) if mode is not None else None
        if mode is None:
            startup_view = "нету"
        elif config_error:
            startup_view = f"ошибка конфигурации ({config_error})"
        else:
            startup_view = f"{mode} ({text})"
        print(ok(BANNER))
        print(ok("Селфбот запущен"))
        print(ok(f"Аккаунт: {bot.user}"))
        print(ok(f"Префикс: {bot.command_prefix}"))
        print(ok(f"Версия: v{VERSION}"))
        print(ok(f"Активность по умолчанию: {startup_view}"))
        bot.startup_banner_printed = True


@bot.command(name="help")
async def help_command(ctx: commands.Context, section: Optional[str] = None) -> None:
    section = (section or "").lower().strip()
    if not section:
        await ctx.message.edit(content=(
            "📘 Помощь\n"
            f"🧩 Версия: v{VERSION}\n\n"
            "📂 Доступные категории:\n"
            "🎮 `.help activity`\n"
            "🛠️ `.help tools`\n"
            "🤖 `.help ai`"
        ))
        return

    if section == "activity":
        await ctx.message.edit(content=(
            "🎮 Категория: Activity\n"
            "`.status playing` — включить игровой статус из game.json\n"
            "`.status streaming` — включить стрим-статус из game.json\n"
            "`.status off` — убрать активность"
        ))
        return

    if section == "tools":
        await ctx.message.edit(content=(
            "🛠️ Категория: Tools\n"
            "`.reloadcfg all` — перезагрузить оба конфига\n"
            "`.reloadcfg config` — перезагрузить только config.json\n"
            "`.reloadcfg game` — перезагрузить только game.json\n"
            "`.reload` — алиас `.reloadcfg all`"
        ))
        return

    if section == "ai":
        await ctx.message.edit(content=(
            "🤖 Категория: AI\n"
            "`.ai <текст>` — отправить запрос в AI\n"
            "Настройка стиля ответа: `config.json -> ai.system_prompt`\n"
            "Доступ другим: `config.json -> ai.allow_others`"
        ))
        return

    await ctx.message.edit(content="❌ Неизвестная категория. Доступно: activity, tools, ai")


@bot.command(name="status")
async def status_command(ctx: commands.Context, mode: Optional[str] = None) -> None:
    if mode is None:
        await ctx.message.edit(content=(
            "Использование:\n"
            ".status playing\n"
            ".status streaming\n"
            ".status off"
        ))
        return

    mode = normalize_status_type(mode)

    if mode in {"off", "reset", "none"}:
        await bot.change_presence(activity=None)
        await ctx.message.edit(content="✅ Статус очищен")
        return

    if mode not in {"playing", "streaming"}:
        await ctx.message.edit(content="❌ Доступно только: playing, streaming, off")
        return

    config_error = bot.validate_mode_config(mode)
    if config_error:
        await ctx.message.edit(content=f"❌ {config_error}. Сначала настрой game.json")
        return

    text = bot.get_status_name_from_config(mode)
    try:
        await bot.apply_status(mode=mode, text=text)
        await ctx.message.edit(content=f"✅ Режим {mode} включен из game.json")
    except Exception as exc:
        await ctx.message.edit(content=f"❌ Ошибка статуса: {exc}. Решение: проверь поля этого режима в game.json и сделай .reloadcfg")


@bot.command(name="reloadcfg", aliases=["reload"])
async def reloadcfg_command(ctx: commands.Context, target: Optional[str] = None) -> None:
    if bot.owner_id is not None and ctx.author.id != bot.owner_id:
        return

    scope = (target or "all").lower().strip()
    if scope not in {"all", "config", "game"}:
        await ctx.message.edit(content="❌ Использование: .reloadcfg [all|config|game]")
        return

    try:
        if scope in {"all", "config"}:
            new_config = load_json(CONFIG_PATH)
        else:
            new_config = bot.config

        if scope in {"all", "game"}:
            new_game = load_json(GAME_CONFIG_PATH)
        else:
            new_game = bot.game_cfg

        ensure_config_values(new_config, new_game)

        bot.config = new_config
        bot.game_cfg = new_game
        bot.command_prefix = str(new_config.get("prefix", "."))
        await bot.restart_rotation_task()
        await bot.restart_stream_keepalive_task()

        if bot.current_status_mode in {"playing", "streaming"}:
            mode = bot.current_status_mode
            config_error = bot.validate_mode_config(mode)
            if config_error is None:
                text = bot.get_status_name_from_config(mode)
                if text:
                    await bot.apply_status(mode=mode, text=text)
        else:
            await bot.apply_startup_status()

        if scope == "all":
            msg = "✅ Перезагружены config.json + game.json"
        elif scope == "config":
            msg = "✅ Перезагружен config.json"
        else:
            msg = "✅ Перезагружен game.json"
        await ctx.message.edit(content=msg)
    except Exception as exc:
        await ctx.message.edit(content=f"❌ Ошибка перезагрузки: {exc}. Решение: проверь JSON-синтаксис и обязательные поля")


@bot.command(name="ai")
async def ai_command(ctx: commands.Context, *, prompt: Optional[str] = None) -> None:
    if not prompt:
        await ctx.message.edit(content="❌ Использование: .ai <текст>")
        return

    ai_cfg = bot.config.get("ai", {})
    if not ai_cfg.get("enabled", True):
        await ctx.message.edit(content="❌ Модуль AI отключен в config.json")
        return

    allow_others = ai_cfg.get("allow_others", False)
    if not allow_others and bot.owner_id is not None and ctx.author.id != bot.owner_id:
        return

    try:
        await ctx.message.edit(content="Думаю...")
        answer = await bot.call_ai(prompt)
        await ctx.send(answer)
    except Exception as exc:
        await ctx.send(f"❌ Ошибка AI: {exc}. Решение: проверь ai.endpoint/ai.api_key и интернет")


@bot.event
async def on_message(message: discord.Message) -> None:
    await bot.process_commands(message)

    ai_cfg = bot.config.get("ai", {})
    allow_others = ai_cfg.get("allow_others", False)
    if not allow_others:
        return

    if bot.user is None:
        return

    if message.author.id == bot.user.id:
        return

    prefix = str(bot.command_prefix)
    trigger = f"{prefix}ai "
    if not message.content.lower().startswith(trigger):
        return

    prompt = message.content[len(trigger):].strip()
    if not prompt:
        return

    try:
        answer = await bot.call_ai(prompt)
        await message.channel.send(answer)
    except Exception as exc:
        await message.channel.send(f"❌ Ошибка AI: {exc}")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"❌ Ошибка команды: {error}. Решение: проверь формат команды в README.")


if __name__ == "__main__":
    try:
        bot.run(str(config["token"]).strip())
    except discord.LoginFailure:
        print(err("Ошибка: неверный токен. Решение: замени token в config.json и перезапусти."))
    except FileNotFoundError as exc:
        print(err(f"Ошибка: {exc}. Решение: проверь наличие config.json и game.json."))
    except ValueError as exc:
        print(err(f"Ошибка конфигурации: {exc}. Решение: исправь конфиги и перезапусти."))
    except Exception as exc:
        print(err(f"Ошибка запуска: {exc}. Решение: проверь конфиги и доступ к интернету."))
