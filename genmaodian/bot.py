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
ARTICLE_MD = """# Do you really want to download this? Are you sure? / 你真的想下载这个吗？确定吗？

## The story of the Download button

In the early web of the 1990s, files were just links. You clicked, and the browser asked where to save. There was no big button, only blue underlined text.

In the 2000s download portals appeared. To stand out, they drew huge green buttons that said **DOWNLOAD**. Soon pages had five of them, and four were ads. People learned to click the biggest, brightest button without reading.

Today the Download button is the most clicked button on the internet, and the least questioned.

## 下载按钮的故事

上世纪九十年代，文件只是一个链接。点击后，浏览器会问你保存在哪里。没有大按钮，只有蓝色的下划线文字。

两千年代，下载站兴起。为了吸引眼球，它们画出巨大的绿色 **下载** 按钮。很快，一个页面上就有五个按钮，其中四个是广告。人们学会了不看内容，直接点击最大、最亮的那个。

今天，下载按钮是互联网上被点击最多、却最少被怀疑的按钮。

## People who download files they don't understand / 下载不了解的文件的人

*Illustrative figures for this article. / 本文示意数据。*

| Group / 人群 | Share / 比例 | Chart / 图表 |
| --- | :---: | --- |
| Click without reading / 不读就点 | 68% | ▓▓▓▓▓▓▓░░░ |
| Read only the title / 只看标题 | 21% | ▓▓░░░░░░░░ |
| Check the source / 检查来源 | 8% | ▓░░░░░░░░░ |
| Read the whole page / 读完全文 | 3% | ░░░░░░░░░░ |

> You made it to the end. You are in the 3%. / 你读到了最后，你属于那 3%。

### 🔑 Password / 密码: ||No2026||
"""

ARTICLE_HTML = (
    "<b>Do you really want to download this? Are you sure?\n你真的想下载这个吗？确定吗？</b>\n\n"
    "<b>The story of the Download button / 下载按钮的故事</b>\n"
    "Files used to be plain links. Then portals drew huge DOWNLOAD buttons, and people learned to click without reading.\n"
    "文件曾经只是链接。后来下载站画出巨大的下载按钮，人们学会了不看就点。\n\n"
    "<pre>Click without reading  68% ▓▓▓▓▓▓▓░░░\n"
    "Read only the title    21% ▓▓░░░░░░░░\n"
    "Check the source        8% ▓░░░░░░░░░\n"
    "Read the whole page     3% ░░░░░░░░░░</pre>\n"
    "<i>Illustrative figures / 示意数据</i>\n\n"
    "<b>🔑 PASSWORD / 密码: <tg-spoiler>No2026</tg-spoiler></b>"
)

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
    await bot.send_message(chat_id, ARTICLE_HTML, reply_markup=markup, parse_mode="HTML")

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
