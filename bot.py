import os, re, asyncio
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import User, UserStatusOnline, UserStatusRecently, UserStatusLastWeek
from telethon.errors import FloodWaitError

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION = os.environ["SESSION"]

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
bot = TelegramClient(StringSession(), API_ID, API_HASH)

OWNER_ID = None

TOGGLES = [
    ("writers", "Только писавшие"),
    ("link", "Ссылка в описании"),
    ("premium", "Premium"),
    ("photo", "Фото профиля"),
    ("has_bio", "Описание не пустое"),
    ("gifts", "Есть подарки"),
    ("channel", "Личный канал"),
    ("active", "В сети за неделю"),
    ("no_scam", "Без scam/fake"),
]

settings = {"chat": "username_чата", "limit": 200, "msgs": 3000,
            "word": "", "stop": "", "name": ""}
for key, _ in TOGGLES:
    settings[key] = False
settings["writers"] = True
settings["link"] = True

running = False

LINK_RE = re.compile(
    r"(https?://|t\.me/|@\w{4,}|\b[\w-]+\.(com|ru|io|net|org|me|xyz|app|pro|info|site|online)\b)",
    re.I,
)


def is_owner(event):
    return event.sender_id == OWNER_ID


def mark(v):
    return "✅" if v else "⬜"


def menu():
    rows, row = [], []
    for key, label in TOGGLES:
        row.append(Button.inline(f"{mark(settings[key])} {label}", f"t:{key}".encode()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("▶️ Запустить", b"run")])
    return rows


def menu_text():
    return (
        f"Чат: {settings['chat']}\n"
        f"Максимум людей для проверки: {settings['limit']}\n"
        f"Просматривать сообщений: {settings['msgs']}\n"
        f"Слово в описании: {settings['word'] or '—'}\n"
        f"Исключить слово: {settings['stop'] or '—'}\n"
        f"Слово в имени: {settings['name'] or '—'}\n\n"
        "Команды:\n"
        "/chat юзернейм\n/limit число\n/msgs число\n"
        "/word слово\n/stop слово\n/name слово\n"
        "(команда без слова = сброс)"
    )


def passes(user, full):
    fu = full.full_user
    bio = fu.about or ""
    s = settings
    if s["link"] and not LINK_RE.search(bio):
        return False
    if s["premium"] and not getattr(user, "premium", False):
        return False
    if s["photo"] and not user.photo:
        return False
    if s["has_bio"] and not bio.strip():
        return False
    if s["gifts"] and not (getattr(fu, "stargifts_count", 0) or 0) > 0:
        return False
    if s["channel"] and not getattr(fu, "personal_channel_id", None):
        return False
    if s["active"] and not isinstance(
        user.status, (UserStatusOnline, UserStatusRecently, UserStatusLastWeek)
    ):
        return False
    if s["no_scam"] and (getattr(user, "scam", False) or getattr(user, "fake", False)):
        return False
    if s["word"] and s["word"].lower() not in bio.lower():
        return False
    if s["stop"] and s["stop"].lower() in bio.lower():
        return False
    if s["name"]:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".lower()
        if s["name"].lower() not in full_name:
            return False
    return True


def message_link(entity, msg_id):
    if not msg_id or entity is None:
        return ""
    uname = getattr(entity, "username", None)
    if uname:
        return f"https://t.me/{uname}/{msg_id}"
    return f"https://t.me/c/{entity.id}/{msg_id}"


async def collect_users():
    users = {}
    if settings["writers"]:
        async for msg in client.iter_messages(settings["chat"], limit=settings["msgs"]):
            u = msg.sender
            if isinstance(u, User) and u.username and not u.bot and not u.deleted:
                if u.id not in users:
                    users[u.id] = (u, msg.id)
                    if len(users) >= settings["limit"]:
                        break
    else:
        async for u in client.iter_participants(settings["chat"], limit=settings["limit"]):
            if u.username and not u.bot and not u.deleted:
                users[u.id] = (u, None)
    return list(users.values())


@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_cmd(event):
    if not is_owner(event):
        return
    await event.respond(menu_text(), buttons=menu())


@bot.on(events.NewMessage(pattern=r"^/chat\s+(\S+)$"))
async def chat_cmd(event):
    if not is_owner(event):
        return
    settings["chat"] = event.pattern_match.group(1).lstrip("@")
    await event.respond(menu_text(), buttons=menu())


@bot.on(events.NewMessage(pattern=r"^/(limit|msgs)\s+(\d+)$"))
async def number_cmd(event):
    if not is_owner(event):
        return
    name = event.pattern_match.group(1)
    value = int(event.pattern_match.group(2))
    settings[name] = min(value, 1000 if name == "limit" else 20000)
    await event.respond(menu_text(), buttons=menu())


@bot.on(events.NewMessage(pattern=r"^/(word|stop|name)(?:\s+(.+))?$"))
async def text_cmd(event):
    if not is_owner(event):
        return
    settings[event.pattern_match.group(1)] = (event.pattern_match.group(2) or "").strip()
    await event.respond(menu_text(), buttons=menu())


async def run_parser(chat_id):
    global running
    if running:
        await bot.send_message(chat_id, "Проверка уже идёт, подождите.")
        return
    running = True
    try:
        await bot.send_message(chat_id, "Собираю людей из чата...")
        users = await collect_users()
        total = len(users)

        try:
            chat_entity = await client.get_entity(settings["chat"])
        except Exception:
            chat_entity = None

        await bot.send_message(
            chat_id,
            f"Людей с юзернеймом: {total}. Проверяю профили "
            f"(около 2 сек на человека, примерно {total * 2 // 60 + 1} мин)...",
        )

        found, checked = [], 0
        gifts_unsupported = False
        for user, msg_id in users:
            checked += 1
            try:
                full = await client(GetFullUserRequest(user))
            except FloodWaitError as e:
                await bot.send_message(
                    chat_id, f"Telegram просит подождать {e.seconds} сек. Жду..."
                )
                await asyncio.sleep(e.seconds + 1)
                try:
                    full = await client(GetFullUserRequest(user))
                except Exception:
                    continue
            except Exception:
                continue
            await asyncio.sleep(1)

            if checked % 50 == 0:
                await bot.send_message(
                    chat_id, f"Проверено {checked} из {total}, найдено {len(found)}"
                )

            if settings["gifts"] and not hasattr(full.full_user, "stargifts_count"):
                gifts_unsupported = True

            if passes(user, full):
                link = message_link(chat_entity, msg_id)
                if link:
                    found.append(f"@{user.username} | {link}")
                else:
                    found.append(f"@{user.username}")

        await bot.send_message(chat_id, f"Проверено: {checked}\nНайдено: {len(found)}")
        if gifts_unsupported:
            await bot.send_message(
                chat_id,
                "Эта версия Telethon не отдаёт данные о подарках, поэтому критерий "
                "«Есть подарки» ничего не найдёт.",
            )
        text = "\n".join(found)
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id, text[i:i + 4000], link_preview=False)
    except Exception as e:
        await bot.send_message(chat_id, f"Ошибка: {e}")
    finally:
        running = False


@bot.on(events.CallbackQuery())
async def on_button(event):
    if not is_owner(event):
        return
    data = event.data.decode()
    if data.startswith("t:"):
        key = data[2:]
        if key in settings:
            settings[key] = not settings[key]
            await event.edit(menu_text(), buttons=menu())
    elif data == "run":
        await event.answer("Запускаю...")
        asyncio.create_task(run_parser(event.chat_id))


async def main():
    global OWNER_ID
    await client.start()
    OWNER_ID = (await client.get_me()).id
    await bot.start(bot_token=BOT_TOKEN)
    print("Бот запущен")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
