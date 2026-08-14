import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = Path(os.getenv("DB_PATH", "links.sqlite3"))
MAX_PHOTO_BYTES = 10 * 1024 * 1024

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
app = FastAPI(docs_url=None, redoc_url=None)

SERVICES = {
    "tiktok": {"name": "TikTok", "emoji": "🎵", "route": "tiktok"},
    "youtube": {"name": "YouTube", "emoji": "📺", "route": "youtube"},
    "telegraph": {"name": "Telegraph", "emoji": "📝", "route": "telegraph"},
}


def db_init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                token TEXT PRIMARY KEY,
                owner_chat_id INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                service TEXT NOT NULL DEFAULT 'tiktok'
            )
            """
        )
        columns = {row[1] for row in con.execute("PRAGMA table_info(links)").fetchall()}
        if "service" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN service TEXT NOT NULL DEFAULT 'tiktok'")
        con.commit()


def create_link(owner_chat_id: int, service: str) -> str:
    if service not in SERVICES:
        service = "tiktok"
    while True:
        token = secrets.token_urlsafe(18)
        try:
            with closing(sqlite3.connect(DB_PATH)) as con:
                con.execute(
                    "INSERT INTO links(token, owner_chat_id, used, service) VALUES (?, ?, 0, ?)",
                    (token, owner_chat_id, service),
                )
                con.commit()
            return token
        except sqlite3.IntegrityError:
            continue


def get_link(token: str):
    with closing(sqlite3.connect(DB_PATH)) as con:
        return con.execute(
            "SELECT owner_chat_id, used, service FROM links WHERE token = ?", (token,)
        ).fetchone()


def claim_link(token: str) -> bool:
    """Temporarily claims an unused link so two uploads cannot race each other."""
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(
            "UPDATE links SET used = 2 WHERE token = ? AND used = 0", (token,)
        )
        con.commit()
        return cur.rowcount == 1


def finish_link(token: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("UPDATE links SET used = 1 WHERE token = ?", (token,))
        con.commit()


def release_link(token: str) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("UPDATE links SET used = 0 WHERE token = ? AND used = 2", (token,))
        con.commit()


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def lookup_ip(ip: str) -> dict:
    if ip in {"unknown", "127.0.0.1", "::1"}:
        return {}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"https://ipwho.is/{ip}")
            r.raise_for_status()
            data = r.json()
            if not data.get("success", True):
                return {}
            return data
    except Exception:
        return {}


def public_link(service: str, token: str) -> str:
    # A real TikTok/YouTube/Telegraph domain cannot route to this server.
    # We therefore use the bot's own HTTPS domain and a service-themed path.
    route = SERVICES.get(service, SERVICES["tiktok"])["route"]
    return f"{PUBLIC_BASE_URL}/{route}/{token}"


def service_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎵 TikTok")],
            [KeyboardButton(text="📺 YouTube")],
            [KeyboardButton(text="📝 Telegraph")],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Выберите оформление страницы. Получатель увидит явное уведомление о том, "
        "что после разрешения камеры и подтверждения отправки фото, IP и примерное "
        "местоположение будут отправлены вам.",
        reply_markup=service_keyboard(),
    )


@router.message(Command("new"))
async def new_link_command(message: Message):
    await message.answer("Выберите оформление новой ссылки:", reply_markup=service_keyboard())


@router.message(F.text.in_({"🎵 TikTok", "📺 YouTube", "📝 Telegraph"}))
async def create_service_link(message: Message):
    service_map = {
        "🎵 TikTok": "tiktok",
        "📺 YouTube": "youtube",
        "📝 Telegraph": "telegraph",
    }
    service = service_map.get(message.text)
    if not service:
        return

    token = create_link(message.chat.id, service)
    url = public_link(service, token)
    info = SERVICES[service]

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔗 Создать новую ссылку")]],
        resize_keyboard=True,
    )
    await message.answer(
        f"{info['emoji']} Одноразовая ссылка создана:\n"
        f"<code>{url}</code>\n\n"
        f"Оформление страницы: {info['name']}.\n"
        "После успешной отправки фото ссылка перестанет работать.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.message(F.text == "🔗 Создать новую ссылку")
async def new_link(message: Message):
    await message.answer("Выберите оформление новой ссылки:", reply_markup=service_keyboard())


def generate_service_page(service: str, token: str) -> str:
    service_styles = {
        "tiktok": {
            "bg": "#010101", "card": "#1a1a1a", "accent": "#00f2ea",
            "text": "#ffffff", "brand": "TikTok", "icon": "🎵",
        },
        "youtube": {
            "bg": "#0a0a0a", "card": "#1a1a1a", "accent": "#ff0000",
            "text": "#ffffff", "brand": "YouTube", "icon": "📺",
        },
        "telegraph": {
            "bg": "#f5f5f5", "card": "#ffffff", "accent": "#2c3e50",
            "text": "#222222", "brand": "Telegraph", "icon": "📝",
        },
    }
    style = service_styles.get(service, service_styles["tiktok"])
    is_light = service == "telegraph"
    token_js = json.dumps(token)
    muted_text = "#555" if is_light else "#ddd"
    small_text = "#888" if is_light else "#aaa"
    notice_bg = "#f8f8f8" if is_light else "rgba(255,255,255,0.05)"
    color_scheme = "light" if is_light else "dark"

    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{style['brand']} — подтверждение фото</title>
<style>
:root {{ color-scheme:{color_scheme}; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:{style['bg']}; color:{style['text']}; }}
.card {{ width:min(92vw,520px); background:{style['card']}; padding:22px; border-radius:22px; box-sizing:border-box; box-shadow:0 8px 32px rgba(0,0,0,.3); }}
.brand {{ display:flex; align-items:center; gap:10px; font-size:28px; font-weight:700; margin-bottom:8px; color:{style['accent']}; }}
h1 {{ font-size:22px; margin:8px 0 12px; }}
p {{ line-height:1.45; color:{muted_text}; }}
.notice {{ padding:14px; border:1px solid {style['accent']}; border-radius:14px; margin:14px 0; background:{notice_bg}; }}
video, canvas {{ width:100%; border-radius:16px; background:#000; margin-top:14px; }}
canvas {{ display:none; }}
button {{ width:100%; border:0; border-radius:14px; padding:15px 16px; font-size:17px; font-weight:700; margin-top:12px; cursor:pointer; }}
button:disabled {{ opacity:.5; cursor:not-allowed; }}
#allow, #snap {{ background:{style['accent']}; color:{'#111' if service == 'tiktok' else '#fff'}; }}
#snap {{ display:none; }}
#send {{ background:#34c759; color:#071b0b; display:none; }}
#status {{ min-height:24px; font-size:14px; margin-top:10px; }}
.small {{ font-size:13px; color:{small_text}; }}
</style>
</head>
<body>
<div class="card">
  <div class="brand"><span>{style['icon']}</span> {style['brand']}</div>
  <h1>Подтверждение фото</h1>
  <div class="notice">
    Если вы продолжите, браузер попросит разрешение на использование камеры.
    Снимок не отправляется автоматически. Только после нажатия «Отправить фото и данные» он будет отправлен человеку, который прислал вам эту ссылку, вместе с вашим IP-адресом и примерными данными о городе, регионе и стране, определёнными по IP.
  </div>
  <p class="small">Доступ к камере включается только после стандартного разрешения браузера. Геолокация по IP приблизительная и может быть неверной.</p>
  <button id="allow">Разрешить камеру</button>
  <video id="video" playsinline autoplay muted></video>
  <button id="snap">Сделать фото</button>
  <canvas id="canvas"></canvas>
  <button id="send">Отправить фото и данные</button>
  <div id="status"></div>
</div>
<script>
const token = {token_js};
const allow = document.getElementById('allow');
const snap = document.getElementById('snap');
const send = document.getElementById('send');
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const status = document.getElementById('status');
let stream = null;
let blob = null;

allow.onclick = async () => {{
  try {{
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
      throw new Error('Камера доступна только по HTTPS в поддерживаемом браузере.');
    }}
    stream = await navigator.mediaDevices.getUserMedia({{video:{{facingMode:'user'}},audio:false}});
    video.srcObject = stream;
    allow.style.display = 'none';
    snap.style.display = 'block';
    status.textContent = 'Камера разрешена. Сделайте фото, когда будете готовы.';
  }} catch (e) {{
    status.textContent = e.message || 'Доступ к камере не предоставлен.';
  }}
}};

snap.onclick = () => {{
  if (!video.videoWidth) {{ status.textContent = 'Камера ещё загружается.'; return; }}
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);
  canvas.style.display = 'block';
  video.style.display = 'none';
  canvas.toBlob(b => {{
    if (!b) {{ status.textContent = 'Не удалось создать снимок.'; return; }}
    blob = b;
    send.style.display = 'block';
  }}, 'image/jpeg', .9);
  snap.textContent = 'Переснять';
  snap.onclick = () => location.reload();
  status.textContent = 'Проверьте снимок. Он ещё не отправлен.';
}};

send.onclick = async () => {{
  if (!blob) return;
  send.disabled = true;
  status.textContent = 'Отправка…';
  const fd = new FormData();
  fd.append('photo', blob, 'photo.jpg');
  try {{
    const r = await fetch(`/api/send/${{encodeURIComponent(token)}}`, {{method:'POST',body:fd}});
    const data = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(data.detail || 'Ошибка отправки');
    status.textContent = 'Фото отправлено. Спасибо.';
    send.style.display = 'none';
    snap.style.display = 'none';
    if (stream) stream.getTracks().forEach(t => t.stop());
  }} catch (e) {{
    status.textContent = e.message || 'Не удалось отправить.';
    send.disabled = false;
  }}
}};
</script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h3>Camera link bot is running.</h3>"


@app.get("/c/{token}", response_class=HTMLResponse)
@app.get("/{service}/{token}", response_class=HTMLResponse)
async def camera_page(token: str, service: str | None = None):
    row = get_link(token)
    if not row:
        raise HTTPException(404, "Ссылка не найдена")
    _owner_chat_id, used, saved_service = row
    if used == 1:
        return HTMLResponse("<h3>Эта одноразовая ссылка уже использована.</h3>", status_code=410)
    if used == 2:
        return HTMLResponse("<h3>Фото по этой ссылке сейчас отправляется.</h3>", status_code=409)

    selected_service = saved_service if saved_service in SERVICES else "tiktok"
    if service in SERVICES and service != selected_service:
        raise HTTPException(404, "Ссылка не найдена")
    return generate_service_page(selected_service, token)


@app.post("/api/send/{token}")
async def send_photo(token: str, request: Request, photo: UploadFile = File(...)):
    row = get_link(token)
    if not row:
        raise HTTPException(404, "Ссылка не найдена")
    owner_chat_id, used, service = row
    if used != 0:
        raise HTTPException(410, "Ссылка уже использована или обрабатывается")

    content_type = (photo.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Разрешены только изображения")

    data = await photo.read(MAX_PHOTO_BYTES + 1)
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Фото слишком большое")

    if not claim_link(token):
        raise HTTPException(410, "Ссылка уже использована или обрабатывается")

    ip = client_ip(request)
    geo = await lookup_ip(ip)
    city = geo.get("city") or "не определён"
    region = geo.get("region") or "не определён"
    country = geo.get("country") or "не определена"
    isp = (geo.get("connection") or {}).get("isp") or "не определён"
    service_emoji = SERVICES.get(service, {}).get("emoji", "📸")

    caption = (
        f"{service_emoji} Получено фото по вашей ссылке\n\n"
        f"🌐 IP: {ip}\n"
        f"🏙 Город: {city}\n"
        f"🗺 Регион: {region}\n"
        f"🌍 Страна: {country}\n"
        f"📡 Провайдер: {isp}\n\n"
        "ℹ️ Местоположение определено приблизительно по IP и может отличаться от фактического."
    )

    try:
        await bot.send_photo(
            chat_id=owner_chat_id,
            photo=BufferedInputFile(data, filename="photo.jpg"),
            caption=caption,
        )
    except Exception as exc:
        release_link(token)
        raise HTTPException(502, "Не удалось доставить фото в Telegram") from exc

    finish_link(token)
    return JSONResponse({"ok": True})


async def main():
    db_init()
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
