# pip install -r requirements.txt  (aiogram>=3.4, aiohttp)
import os, datetime
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

TOKEN = os.getenv("BOT_TOKEN")
ZIP_PATH = "archive.zip"
K = 153

# Rich Message article (Bot API 10.1+, Markdown dialect). EN + 中文 only.
ARTICLE_MD = """# 根锚点下载 · Download Agreement

## English

Thank you for using **根锚点下载**. Before you receive the archive, please read this short notice.

| Item | Detail |
| --- | --- |
| License | Free for personal use |
| Platforms | Windows / Mac / Android / iOS |
| Size | ~1.1 MB |
| Updated | 2026-10-13 |

- No bundled software.
- No ads or trackers.
- Delete within 24h if you do not agree.

> By tapping **Agree** you accept these terms and the ZIP will be sent to you.

## 中文

感谢使用 **根锚点下载**。领取压缩包前，请阅读以下说明。

| 项目 | 说明 |
| --- | --- |
| 授权 | 个人免费使用 |
| 平台 | Windows / Mac / Android / iOS |
| 大小 | 约 1.1 MB |
| 更新 | 2026-10-13 |

- 无捆绑软件。
- 无广告、无追踪。
- 如不同意请在 24 小时内删除。

> 点击 **同意 / Agree** 即表示接受条款，机器人会把 ZIP 发送给你。
"""

POINTS = [(1, 20130801), (100_000_000, 20150101), (500_000_000, 20180101),
          (1_000_000_000, 20191201), (2_000_000_000, 20210601),
          (5_000_000_000, 20220301), (7_000_000_000, 20240401), (8_000_000_000, 20250701)]

def to_date(n): return datetime.date(n // 10000, n // 100 % 100, n % 100)

def creation_date(uid: int) -> int:
    for (i1, d1), (i2, d2) in zip(POINTS, POINTS[1:]):
        if uid <= i2:
            a, b = to_date(d1), to_date(d2)
            d = a + (b - a) * ((uid - i1) / (i2 - i1))
            return int(d.strftime("%Y%m%d"))
    return POINTS[-1][1]

def make_key(uid: int) -> str:
    today = int(datetime.date.today().strftime("%Y%m%d"))
    return f"{uid*K}-{creation_date(uid)*K}-{today*K}"

dp = Dispatcher()

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Your key / 你的密钥:\n"
        f"<code>{make_key(m.from_user.id)}</code>\n\n"
        "Valid today only. Paste it on the website. / 当天有效，请粘贴到网站。",
        parse_mode="HTML")

async def send_rich_or_fallback(bot: Bot, chat_id: int, markup):
    """Try Rich Message (Bot API 10.1+). Fall back to plain text if unavailable."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendRichMessage"
    payload = {"chat_id": chat_id, "rich_message": {"markdown": ARTICLE_MD},
               "reply_markup": markup.model_dump(exclude_none=True)}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload) as r:
                if r.status == 200 and (await r.json()).get("ok"):
                    return
    except Exception:
        pass
    await bot.send_message(chat_id, ARTICLE_MD, reply_markup=markup)

@dp.message(Command("download"))
async def download(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Agree / 同意", callback_data="agree"),
        InlineKeyboardButton(text="❌ Decline / 拒绝", callback_data="decline")]])
    await send_rich_or_fallback(m.bot, m.chat.id, kb)

@dp.callback_query(F.data == "agree")
async def agree(c: CallbackQuery):
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    if os.path.exists(ZIP_PATH):
        await c.message.answer_document(FSInputFile(ZIP_PATH))
    else:
        await c.message.answer("Archive coming soon / 压缩包即将上线")
    await c.answer()

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery):
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass
    await c.message.answer("Cancelled / 已取消")
    await c.answer()

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "")
PATH = "/tg-webhook"

async def on_startup(bot: Bot):
    if BASE_URL:
        await bot.set_webhook(BASE_URL + PATH, drop_pending_updates=False)

def main():
    app = web.Application()
    try:
        bot = Bot(TOKEN)
    except Exception as e:
        print("BOT_TOKEN is missing or invalid. Set it in Render -> Environment.", e)
        app.router.add_get("/", lambda r: web.Response(text="BOT_TOKEN not set"))
        web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
        return
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    dp.startup.register(on_startup)
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

if __name__ == "__main__":
    main()
