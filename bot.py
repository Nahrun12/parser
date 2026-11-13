import os, re, json, asyncio, html
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events, Button, functions
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel
from telethon.errors import FloodWaitError

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION = os.environ["SESSION"]

DATA_DIR = (
    os.environ.get("DATA_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or "."
)
STATE_FILE = os.path.join(DATA_DIR, "state.json")

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
bot = TelegramClient(StringSession(), API_ID, API_HASH)

OWNER_ID = None
running = False
stop_flag = False
tasks = set()
seen = set()

# Критерии с тремя состояниями: any (не важно), yes (нужно), no (не должно быть)
TRI = [
    ("link", "Ссылка в описании"),
    ("premium", "Premium"),
    ("photo", "Фото профиля"),
    ("has_bio", "Описание заполнено"),
    ("channel", "Личный канал"),
]
TRI_MARK = {"any": "➖", "yes": "✅", "no": "❌"}

GIFT_COUNT = [("any", "Любое"), ("none", "Нет"), ("lt3", "Меньше 3"), ("ge3", "3 и больше")]
GIFT_NFT = [("any", "Не важно"), ("yes", "Есть NFT"), ("no", "Нет NFT")]
GIFT_PERIOD = [("any", "Любой"), ("1", "Сегодня"), ("3", "3 дня"), ("7", "Неделя"), ("30", "Месяц")]

settings = {
    "chats": [],
    "limit": 200,
    "msgs": 3000,
    "writers": True,
    "hide_seen": True,
    "word": "",
    "exclude": "",
    "name": "",
    "msgword": "",
    "gift_count": "any",
    "gift_nft": "any",
    "gift_period": "any",
}
for _key, _ in TRI:
    settings[_key] = "any"
settings["link"] = "yes"

LINK_RE = re.compile(
    r"(https?://|t\.me/|@\w{4,}|\b[\w-]+\.(com|ru|io|net|org|me|xyz|app|pro|info|site|online)\b)",
    re.I,
)


# ---------- сохранение ----------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.get("settings", {}).items():
            if k in settings:
                settings[k] = v
        seen.update(data.get("seen", []))
    except Exception:
        pass


def save_state():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"settings": settings, "seen": sorted(seen)}, f, ensure_ascii=False)
    except Exception as e:
        print("Не удалось сохранить состояние:", e)


# ---------- вспомогательное ----------
def is_owner(event):
    return event.sender_id == OWNER_ID


def mark(v):
    return "✅" if v else "⬜"


def label(options, value):
    return dict(options).get(value, value)


def parse_chats(text):
    out = []
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        p = part.strip()
        if not p:
            continue
        p = re.sub(r"^https?://t\.me/", "", p)
        p = re.sub(r"^t\.me/", "", p)
        p = p.lstrip("@").split("?")[0].rstrip("/")
        if p and p not in out:
            out.append(p)
    return out


async def wait(seconds):
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not stop_flag and loop.time() < end:
        await asyncio.sleep(1)


def message_link(entity, msg_id):
    if not msg_id or not isinstance(entity, Channel):
        return ""
    uname = getattr(entity, "username", None)
    if uname:
        return f"https://t.me/{uname}/{msg_id}"
    return f"https://t.me/c/{entity.id}/{msg_id}"


async def resolve_chat(chat):
    """Открывает чат по юзернейму или числовому id (в том числе закрытый)."""
    target = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
    try:
        return await client.get_entity(target)
    except Exception:
        if isinstance(target, int):
            # для id Telegram отдаёт доступ только после загрузки списка диалогов
            await client.get_dialogs(limit=None)
            return await client.get_entity(target)
        raise


# ---------- меню ----------
def gifts_summary():
    s = settings
    if s["gift_count"] == "any" and s["gift_nft"] == "any" and s["gift_period"] == "any":
        return "не важно"
    parts = []
    if s["gift_count"] != "any":
        parts.append("количество: " + label(GIFT_COUNT, s["gift_count"]).lower())
    if s["gift_nft"] != "any":
        parts.append(label(GIFT_NFT, s["gift_nft"]).lower())
    if s["gift_period"] != "any":
        parts.append("получены: " + label(GIFT_PERIOD, s["gift_period"]).lower())
    return ", ".join(parts)


def menu():
    rows, row = [], []
    for key, name in TRI:
        row.append(Button.inline(f"{TRI_MARK[settings[key]]} {name}", f"c:{key}".encode()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("🎁 Подарки…", b"g")])
    rows.append([
        Button.inline(f"{mark(settings['writers'])} Только писавшие", b"w:writers"),
        Button.inline(f"{mark(settings['hide_seen'])} Скрывать найденных", b"w:hide_seen"),
    ])
    rows.append([Button.inline("▶️ Запустить", b"run")])
    return rows


def menu_text():
    s = settings
    return (
        f"Чаты: {', '.join(s['chats']) or 'не заданы'}\n"
        f"Макс. новых людей на чат: {s['limit']}\n"
        f"Просматривать сообщений: {s['msgs']}\n"
        f"Слово в описании: {s['word'] or '—'}\n"
        f"Исключить слово: {s['exclude'] or '—'}\n"
        f"Слово в имени: {s['name'] or '—'}\n"
        f"Слово в сообщениях: {s['msgword'] or '—'}\n"
        f"Подарки: {gifts_summary()}\n"
        f"Уже найдено ранее: {len(seen)}\n\n"
        "✅ нужно   ❌ не должно быть   ➖ не важно\n\n"
        "Команды:\n"
        "/chat имя или id — задать чаты (можно несколько)\n"
        "/mychats — показать мои группы и их id\n"
        "/addchat имя, /delchat имя\n"
        "/limit число, /msgs число\n"
        "/word, /exclude, /name, /msgword слово\n"
        "(команда без слова = сброс)\n"
        "/clearseen — забыть найденных\n"
        "/stop — остановить проверку"
    )


def option_rows(prefix, options, current, per_row):
    buttons = [
        Button.inline(("✅ " if value == current else "") + name, f"{prefix}:{value}".encode())
        for value, name in options
    ]
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def gifts_menu():
    s = settings
    rows = [[Button.inline("— Количество подарков —", b"noop")]]
    rows += option_rows("gc", GIFT_COUNT, s["gift_count"], 2)
    rows.append([Button.inline("— NFT-подарки —", b"noop")])
    rows += option_rows("gn", GIFT_NFT, s["gift_nft"], 3)
    rows.append([Button.inline("— Когда получен подарок —", b"noop")])
    rows += option_rows("gp", GIFT_PERIOD, s["gift_period"], 3)
    rows.append([Button.inline("⬅️ Назад", b"back")])
    return rows


def gifts_text():
    return (
        "🎁 Фильтр по подаркам\n\n"
        f"Сейчас: {gifts_summary()}\n\n"
        "«Меньше 3» значит от 1 до 2 (для нуля есть «Нет»). "
        "Если выбран период, считаются только подарки, полученные за него. "
        "Проверяются подарки, видимые в профиле (до 100 последних)."
    )


async def safe_edit(event, text, buttons):
    try:
        await event.edit(text, buttons=buttons, parse_mode=None)
    except Exception:
        pass


# ---------- проверки ----------
def tri_ok(mode, value):
    if mode == "yes":
        return bool(value)
    if mode == "no":
        return not value
    return True


def profile_ok(user, fu):
    s = settings
    bio = fu.about or ""
    checks = [
        ("link", bool(LINK_RE.search(bio))),
        ("premium", bool(getattr(user, "premium", False))),
        ("photo", bool(user.photo)),
        ("has_bio", bool(bio.strip())),
        ("channel", bool(getattr(fu, "personal_channel_id", None))),
    ]
    for key, value in checks:
        if not tri_ok(s[key], value):
            return False
    low = bio.lower()
    if s["word"] and s["word"].lower() not in low:
        return False
    if s["exclude"] and s["exclude"].lower() in low:
        return False
    if s["name"]:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".lower()
        if s["name"].lower() not in full_name:
            return False
    return True


async def fetch_gifts(user):
    req = getattr(functions.payments, "GetSavedStarGiftsRequest", None)
    if req is None:
        return None  # версия Telethon не поддерживает
    try:
        res = await client(req(peer=user, offset="", limit=100))
    except FloodWaitError as e:
        await wait(e.seconds + 1)
        try:
            res = await client(req(peer=user, offset="", limit=100))
        except Exception:
            return []
    except Exception:
        return []
    items = []
    for g in getattr(res, "gifts", None) or []:
        d = getattr(g, "date", None)
        if isinstance(d, (int, float)):
            d = datetime.fromtimestamp(d, tz=timezone.utc)
        elif isinstance(d, datetime) and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        unique = type(getattr(g, "gift", None)).__name__ == "StarGiftUnique"
        items.append((d, unique))
    return items


async def gifts_ok(user, fu):
    """Возвращает (подходит, версия_не_поддерживает)."""
    s = settings
    count_mode, nft_mode, period = s["gift_count"], s["gift_nft"], s["gift_period"]
    if count_mode == "any" and nft_mode == "any" and period == "any":
        return True, False

    if nft_mode != "any" or period != "any":
        items = await fetch_gifts(user)
        await asyncio.sleep(0.5)
        if items is None:
            return False, True
        if period != "any":
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(period))
            items = [(d, u) for d, u in items if d and d >= cutoff]
        n = len(items)
        has_nft = any(u for _, u in items)
    else:
        if not hasattr(fu, "stargifts_count"):
            return False, True  # библиотека не знает это поле
        n = fu.stargifts_count or 0  # None = подарков в профиле нет или они скрыты
        has_nft = False

    if nft_mode == "yes" and not has_nft:
        return False, False
    if nft_mode == "no" and has_nft:
        return False, False
    if count_mode == "none" and n != 0:
        return False, False
    if count_mode == "lt3" and not (1 <= n <= 2):
        return False, False
    if count_mode == "ge3" and n < 3:
        return False, False
    if period != "any" and count_mode == "any" and n == 0:
        return False, False
    return True, False


# ---------- сбор людей ----------
async def collect_users(target):
    """Возвращает список (user, id_сообщения, сколько_раз_писал).

    Сообщения считаются только в пределах /msgs последних сообщений чата.
    """
    users = {}
    counts = {}
    msgword = settings["msgword"].lower()
    if settings["writers"]:
        async for msg in client.iter_messages(target, limit=settings["msgs"]):
            if stop_flag:
                break
            u = msg.sender
            if not (isinstance(u, User) and u.username and not u.bot and not u.deleted):
                continue
            counts[u.id] = counts.get(u.id, 0) + 1
            if msgword and msgword not in (msg.raw_text or "").lower():
                continue
            if u.id in users or len(users) >= settings["limit"]:
                continue
            if settings["hide_seen"] and u.id in seen:
                continue
            users[u.id] = (u, msg.id)
        return [(u, m, counts.get(u.id, 0)) for u, m in users.values()]

    async for u in client.iter_participants(target, limit=settings["limit"]):
        if stop_flag:
            break
        if u.username and not u.bot and not u.deleted:
            if settings["hide_seen"] and u.id in seen:
                continue
            users[u.id] = (u, None)
    return [(u, m, None) for u, m in users.values()]


# ---------- результат ----------
def plural_msgs(n):
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} сообщение"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} сообщения"
    return f"{n} сообщений"


async def send_results(chat_id, results, checked, gifts_unsupported, stopped):
    items = [it for _, found in results for it in found]
    head = ("Остановлено. " if stopped else "") + f"Проверено: {checked}\nНайдено: {len(items)}"
    if any(it[3] is not None for it in items):
        head += f"\nСообщения считаются среди последних {settings['msgs']} сообщений чата."
    await bot.send_message(chat_id, head, parse_mode=None)
    if gifts_unsupported:
        await bot.send_message(
            chat_id,
            "Эта версия Telethon не отдаёт нужные данные о подарках, поэтому "
            "фильтр по подаркам ничего не находит.",
            parse_mode=None,
        )

    lines = []
    for n, (username, title, link, cnt) in enumerate(items, 1):
        name = html.escape(title)
        place = f'<a href="{html.escape(link, quote=True)}">{name}</a>' if link else name
        line = f"{n}. @{username} — {place}"
        if cnt is not None:
            line += f" — {plural_msgs(cnt)}"
        lines.append(line)

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await bot.send_message(chat_id, chunk, link_preview=False, parse_mode="html")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await bot.send_message(chat_id, chunk, link_preview=False, parse_mode="html")


async def run_parser(chat_id):
    global running, stop_flag
    if running:
        await bot.send_message(chat_id, "Проверка уже идёт. Остановить: /stop")
        return
    if not settings["chats"]:
        await bot.send_message(chat_id, "Сначала задайте чаты: /chat имя1 имя2")
        return
    running = True
    stop_flag = False
    results = []
    checked_total = 0
    gifts_unsupported = False
    processed = set()
    try:
        await bot.send_message(
            chat_id,
            f"Начинаю проверку. Чатов: {len(settings['chats'])}",
            buttons=[[Button.inline("⏹ Остановить", b"stop")]],
        )
        for chat in list(settings["chats"]):
            if stop_flag:
                break
            try:
                entity = await resolve_chat(chat)
            except Exception as e:
                hint = ""
                if chat.startswith("+") or "joinchat" in chat:
                    hint = (
                        " Это пригласительная ссылка: вступите в группу вручную "
                        "и добавьте её через /mychats."
                    )
                await bot.send_message(
                    chat_id, f"Чат {chat}: не удалось открыть ({e}).{hint}", parse_mode=None
                )
                continue
            title = getattr(entity, "title", None) or chat
            await bot.send_message(chat_id, f"Чат {title}: собираю людей...", parse_mode=None)
            try:
                users = await collect_users(entity)
            except Exception as e:
                await bot.send_message(chat_id, f"Чат {title}: ошибка сбора ({e})", parse_mode=None)
                continue
            users = [(u, m, c) for u, m, c in users if u.id not in processed]
            total = len(users)
            await bot.send_message(
                chat_id,
                f"Чат {title}: к проверке {total} (примерно {total * 2 // 60 + 1} мин)",
                parse_mode=None,
            )

            found = []
            for i, (user, msg_id, cnt) in enumerate(users, 1):
                if stop_flag:
                    break
                processed.add(user.id)
                checked_total += 1
                try:
                    full = await client(functions.users.GetFullUserRequest(user))
                except FloodWaitError as e:
                    await bot.send_message(chat_id, f"Telegram просит подождать {e.seconds} сек. Жду...")
                    await wait(e.seconds + 1)
                    if stop_flag:
                        break
                    try:
                        full = await client(functions.users.GetFullUserRequest(user))
                    except Exception:
                        continue
                except Exception:
                    continue
                await asyncio.sleep(1)

                if i % 50 == 0:
                    await bot.send_message(
                        chat_id, f"{title}: проверено {i} из {total}, найдено {len(found)}", parse_mode=None
                    )

                fu = full.full_user
                if not profile_ok(user, fu):
                    continue
                ok, unsupported = await gifts_ok(user, fu)
                if unsupported:
                    gifts_unsupported = True
                if not ok:
                    continue

                link = message_link(entity, msg_id)
                found.append((user.username, title, link, cnt))
                seen.add(user.id)
            results.append((title, found))
    except Exception as e:
        await bot.send_message(chat_id, f"Ошибка: {e}", parse_mode=None)
    finally:
        stopped = stop_flag
        save_state()
        try:
            await send_results(chat_id, results, checked_total, gifts_unsupported, stopped)
        except Exception as e:
            print("Не удалось отправить результат:", e)
        running = False
        stop_flag = False


# ---------- команды ----------
@bot.on(events.NewMessage(pattern=r"^/(start|menu)$"))
async def start_cmd(event):
    if not is_owner(event):
        return
    await event.respond(menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/chat(?:\s+(.+))?$"))
async def chat_cmd(event):
    if not is_owner(event):
        return
    settings["chats"] = parse_chats(event.pattern_match.group(1))
    save_state()
    await event.respond(menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/addchat\s+(.+)$"))
async def addchat_cmd(event):
    if not is_owner(event):
        return
    for c in parse_chats(event.pattern_match.group(1)):
        if c not in settings["chats"]:
            settings["chats"].append(c)
    save_state()
    await event.respond(menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/delchat\s+(.+)$"))
async def delchat_cmd(event):
    if not is_owner(event):
        return
    remove = {c.lower() for c in parse_chats(event.pattern_match.group(1))}
    settings["chats"] = [c for c in settings["chats"] if c.lower() not in remove]
    save_state()
    await event.respond(menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/(limit|msgs)\s+(\d+)$"))
async def number_cmd(event):
    if not is_owner(event):
        return
    name = event.pattern_match.group(1)
    value = int(event.pattern_match.group(2))
    settings[name] = min(value, 1000 if name == "limit" else 20000)
    save_state()
    await event.respond(menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/(word|exclude|name|msgword)(?:\s+(.+))?$"))
async def text_cmd(event):
    if not is_owner(event):
        return
    settings[event.pattern_match.group(1)] = (event.pattern_match.group(2) or "").strip()
    save_state()
    await event.respond(menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/clearseen$"))
async def clearseen_cmd(event):
    if not is_owner(event):
        return
    seen.clear()
    save_state()
    await event.respond("Список найденных очищен.\n\n" + menu_text(), buttons=menu(), parse_mode=None)


@bot.on(events.NewMessage(pattern=r"^/mychats(?:\s+(.+))?$"))
async def mychats_cmd(event):
    if not is_owner(event):
        return
    query = (event.pattern_match.group(1) or "").strip().lower()
    await event.respond("Загружаю список ваших групп...", parse_mode=None)
    try:
        dialogs = await client.get_dialogs(limit=None)
    except Exception as e:
        await event.respond(f"Не удалось получить список: {e}", parse_mode=None)
        return

    blocks = []
    for d in dialogs:
        if not (d.is_group or d.is_channel):
            continue
        title = d.name or "Без названия"
        if query and query not in title.lower():
            continue
        is_broadcast = d.is_channel and not d.is_group
        kind = "канал" if is_broadcast else "группа"
        uname = getattr(d.entity, "username", None)
        ident = uname if uname else d.id
        lock = "" if uname else "🔒 "
        blocks.append(
            f"{lock}<b>{html.escape(title)}</b> ({kind})\n"
            f"<code>/addchat {ident}</code>"
        )

    if not blocks:
        await event.respond("Ничего не найдено.", parse_mode=None)
        return

    total = len(blocks)
    shown = blocks[:150]
    head = f"Найдено: {total}"
    if total > len(shown):
        head += f" (показаны первые {len(shown)}, уточните поиск: /mychats слово)"
    await event.respond(
        head + "\nНажмите на команду, чтобы скопировать, и отправьте её боту.", parse_mode=None
    )

    chunk = ""
    for block in shown:
        if len(chunk) + len(block) + 2 > 3500:
            await event.respond(chunk, parse_mode="html")
            chunk = ""
        chunk += block + "\n\n"
    if chunk.strip():
        await event.respond(chunk, parse_mode="html")


@bot.on(events.NewMessage(pattern=r"^/stop$"))
async def stop_cmd(event):
    global stop_flag
    if not is_owner(event):
        return
    if running:
        stop_flag = True
        await event.respond("Останавливаю...")
    else:
        await event.respond("Проверка сейчас не идёт.")


# ---------- кнопки ----------
@bot.on(events.CallbackQuery())
async def on_button(event):
    global stop_flag
    if not is_owner(event):
        return
    data = event.data.decode()

    if data == "noop":
        await event.answer()
    elif data.startswith("c:"):
        key = data[2:]
        if key in dict(TRI):
            order = ["any", "yes", "no"]
            settings[key] = order[(order.index(settings[key]) + 1) % 3]
            save_state()
            await safe_edit(event, menu_text(), menu())
    elif data.startswith("w:"):
        key = data[2:]
        if key in ("writers", "hide_seen"):
            settings[key] = not settings[key]
            save_state()
            await safe_edit(event, menu_text(), menu())
    elif data == "g":
        await safe_edit(event, gifts_text(), gifts_menu())
    elif data == "back":
        await safe_edit(event, menu_text(), menu())
    elif data.startswith(("gc:", "gn:", "gp:")):
        prefix, value = data.split(":", 1)
        options, key = {
            "gc": (GIFT_COUNT, "gift_count"),
            "gn": (GIFT_NFT, "gift_nft"),
            "gp": (GIFT_PERIOD, "gift_period"),
        }[prefix]
        if value in dict(options):
            settings[key] = value
            save_state()
            await safe_edit(event, gifts_text(), gifts_menu())
    elif data == "run":
        await event.answer("Запускаю...")
        task = asyncio.create_task(run_parser(event.chat_id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    elif data == "stop":
        if running:
            stop_flag = True
            await event.answer("Останавливаю...")
        else:
            await event.answer("Проверка не идёт.")


async def main():
    global OWNER_ID
    load_state()
    await client.start()
    OWNER_ID = (await client.get_me()).id
    try:
        await client.get_dialogs(limit=300)
    except Exception as e:
        print("Не удалось загрузить диалоги:", e)
    await bot.start(bot_token=BOT_TOKEN)
    print("Бот запущен")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
