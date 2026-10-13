# pip install aiogram==3.*
import asyncio, os, datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

TOKEN = os.getenv("BOT_TOKEN")          # токен от @BotFather
ZIP_PATH = "files/archive.zip"          # ваш архив
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
    await c.message.answer_document(FSInputFile(ZIP_PATH))
    await c.answer()

@dp.callback_query(F.data == "decline")
async def decline(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Отменено / 已取消 / Cancelled")
    await c.answer()

async def main():
    await dp.start_polling(Bot(TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
