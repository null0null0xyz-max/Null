# pip install aiogram==3.* aiohttp
import os, datetime
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

TOKEN = os.getenv("BOT_TOKEN")          # токен от @BotFather
ZIP_PATH = "archive.zip"          # ваш архив
ARTICLE = "Здесь текст статьи / 文章内容 / Article text"
K = 153

# Опорные точки ID -> дата регистрации (приблизительно). Bot API не отдаёт дату создания аккаунта.
POINTS = [(1, 20130801), (100_000_000, 20150101), (500_000_000, 20180101),
          (1_000_000_000, 20191201), (2_000_000_000, 20210601),
          (5_000_000_000, 20220301), (6_000_000_000, 20230101), (7_000_000_000, 20240401),
          (8_000_000_000, 20250701)]

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
    await m.answer(f"Ваш ключ / 你的密钥 / Your key:\n<code>{make_key(m.from_user.id)}</code>\n\n"
                   "Действует до конца дня. / 当天有效。 / Valid today only.", parse_mode="HTML")

@dp.message(Command("download"))
async def download(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Согласен / 同意 / Agree", callback_data="agree"),
        InlineKeyboardButton(text="❌ Отказаться / 拒绝 / Decline", callback_data="decline")]])
    await m.answer(ARTICLE, reply_markup=kb)

@dp.callback_query(F.data == "agree")
async def agree(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=None)
    if os.path.exists(ZIP_PATH):
        await c.message.answer_document(FSInputFile(ZIP_PATH))
    else:
        await c.message.answer("Архив скоро появится / 压缩包即将上线 / Archive coming soon")
    await c.answer()

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Отменено / 已取消 / Cancelled")
    await c.answer()

# Webhook mode: works on a free Render web service (wakes on each Telegram update)
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "")
PATH = "/tg-webhook"

async def on_startup(bot: Bot):
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
    dp.startup.register(on_startup)
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

if __name__ == "__main__":
    main()
