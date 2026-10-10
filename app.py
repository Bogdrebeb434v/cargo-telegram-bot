import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0") or 0)
PROFILE_PATH = Path(os.getenv("PROFILE_PATH", "profile.json"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("personal-cargo-card")

DEFAULT = {
    "name": "Ваше имя / название компании",
    "phone": "+7 XXX XXX-XX-XX",
    "telegram": "@your_username",
    "about": "Грузоперевозки по городу и области.",
    "vehicle": "Фургон до 800 кг, объём до 5 м³; кузов 2,50 × 1,25 × 1,23 м (Д × Ш × В).",
    "services": "Перевозка мебели, коробок, бытовой техники, товаров и небольших грузов.",
    "work_area": "Москва и Московская область",
    "schedule": "По договорённости",
    "notes": "Стоимость предварительная и подтверждается после уточнения условий.",
    "base_address": "Москва",
    "base_fee": 900,
    "per_km_empty": 35,
    "per_km_loaded": 50,
    "minimum": 2200,
    "included_loaded_km": 0,
    "currency": "₽"
}


def save_profile(profile):
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def load_profile():
    if PROFILE_PATH.exists():
        try:
            old = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            profile = {**DEFAULT, **old}
            # Keep the requested vehicle dimensions visible in the default card if
            # the older profile still contains the previous shorter description.
            if profile.get("vehicle") == "Фургон до 800 кг, объём до 5 м³":
                profile["vehicle"] = DEFAULT["vehicle"]
            save_profile(profile)
            return profile
        except Exception:
            log.exception("Не удалось прочитать profile.json")
    save_profile(DEFAULT)
    return DEFAULT.copy()


p = load_profile()
bot = Bot(TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())


def owner(user_id):
    # Fail closed: if owner ID is not configured, nobody can access admin controls.
    return OWNER_ID != 0 and user_id == OWNER_ID


def esc(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def menu(user_id):
    rows = [
        [InlineKeyboardButton(text="🧮 Рассчитать стоимость", callback_data="calc")],
        [InlineKeyboardButton(text="🚚 Моя визитка и услуги", callback_data="card")],
        [InlineKeyboardButton(text="📞 Связаться со мной", callback_data="contact")],
    ]
    if owner(user_id):
        rows.extend([
            [InlineKeyboardButton(text="✏️ Изменить визитку", callback_data="edit")],
            [InlineKeyboardButton(text="₽ Настроить тарифы", callback_data="rates")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def username_url():
    username = str(p.get("telegram", "")).strip().lstrip("@")
    if username and username != "your_username" and all(ch.isalnum() or ch == "_" for ch in username):
        return f"https://t.me/{username}"
    return None


def card_text():
    return (
        f"🚚 <b>{esc(p['name'])}</b>\n"
        f"{esc(p['about'])}\n\n"
        f"<b>Услуги:</b> {esc(p['services'])}\n"
        f"<b>Автомобиль:</b> {esc(p['vehicle'])}\n"
        f"<b>География:</b> {esc(p['work_area'])}\n"
        f"<b>График:</b> {esc(p['schedule'])}\n\n"
        f"📞 <b>Телефон:</b> {esc(p['phone'])}\n"
        f"✈️ <b>Telegram:</b> {esc(p['telegram'])}\n\n"
        f"<i>{esc(p['notes'])}</i>"
    )


def contact_keyboard():
    rows = []
    t_url = username_url()
    if t_url:
        rows.append([InlineKeyboardButton(text="✈️ Написать в Telegram", url=t_url)])
    share_text = f"Грузоперевозки: {p['name']} · Телефон: {p['phone']} · Telegram: {p['telegram']}"
    rows.append([InlineKeyboardButton(
        text="📤 Поделиться контактами",
        url=f"https://t.me/share/url?url={quote(share_text)}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def geocode(address):
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "PersonalCargoCardBot/1.0"}) as client:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address + ", Россия", "format": "jsonv2", "limit": 1}
        )
        response.raise_for_status()
        data = response.json()
        return (float(data[0]["lat"]), float(data[0]["lon"])) if data else None


async def route(start, end):
    try:
        a, b = await geocode(start), await geocode(end)
        if not a or not b:
            return None, None, "Не удалось определить адрес."
        url = f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
        async with httpx.AsyncClient(timeout=18) as client:
            response = await client.get(url, params={"overview": "false"})
            response.raise_for_status()
            data = response.json()
        if not data.get("routes"):
            return None, None, "Маршрут не найден."
        route_data = data["routes"][0]
        return round(route_data["distance"] / 1000, 1), round(route_data["duration"] / 60), None
    except Exception as exc:
        log.warning("Ошибка маршрута: %s", exc)
        return None, None, "Сервис маршрутов временно недоступен."


def map_url(start, pickup, delivery):
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(start)}&destination={quote(delivery)}"
        f"&waypoints={quote(pickup)}&travelmode=driving"
    )


def calc_price(empty_km, loaded_km):
    empty_cost = round(empty_km * int(p["per_km_empty"]))
    loaded_cost = round(max(0, loaded_km - int(p["included_loaded_km"])) * int(p["per_km_loaded"]))
    total = max(int(p["minimum"]), int(p["base_fee"]) + empty_cost + loaded_cost)
    return int(round(total / 10) * 10), empty_cost, loaded_cost


class Calc(StatesGroup):
    pickup = State()
    delivery = State()
    cargo = State()
    result = State()


class Edit(StatesGroup):
    value = State()


class Rates(StatesGroup):
    value = State()


PROFILE_FIELDS = {
    "name": "Имя / название компании",
    "phone": "Телефон",
    "telegram": "Telegram username",
    "about": "Короткое описание",
    "services": "Услуги",
    "vehicle": "Автомобиль и размеры",
    "work_area": "География",
    "schedule": "График работы",
    "notes": "Примечание клиенту",
    "base_address": "Точка старта автомобиля для расчёта (клиентам не показывается)"
}
RATE_FIELDS = {
    "base_fee": "Фиксированная часть подачи, ₽",
    "per_km_empty": "Подача без груза, ₽/км",
    "per_km_loaded": "Перевозка с грузом, ₽/км",
    "minimum": "Минимальный заказ, ₽",
    "included_loaded_km": "Км с грузом, включённые в цену"
}


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Здравствуйте! Это визитка и калькулятор грузоперевозок.\n"
        "Посмотрите услуги, свяжитесь с перевозчиком или рассчитайте предварительную стоимость маршрута.",
        reply_markup=menu(message.from_user.id)
    )


@dp.message(Command("card"))
async def command_card(message: Message):
    await message.answer(card_text(), parse_mode="HTML", reply_markup=contact_keyboard())


@dp.message(Command("calc"))
async def command_calc(message: Message, state: FSMContext):
    await begin_calc(message, state)


@dp.callback_query(F.data == "card")
async def show_card(callback: CallbackQuery):
    rows = []
    t_url = username_url()
    if t_url:
        rows.append([InlineKeyboardButton(text="✈️ Написать в Telegram", url=t_url)])
    rows.append([InlineKeyboardButton(text="📞 Контакты", callback_data="contact")])
    rows.append([InlineKeyboardButton(text="🧮 Рассчитать стоимость", callback_data="calc")])
    rows.append([InlineKeyboardButton(
        text="📤 Поделиться визиткой",
        url=f"https://t.me/share/url?url={quote('https://t.me/' + (await bot.get_me()).username)}"
        f"&text={quote('Грузоперевозки: ' + p['name'] + '. Откройте визитку и рассчитайте стоимость.')}"
    )])
    await callback.message.answer(
        card_text(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@dp.callback_query(F.data == "contact")
async def show_contact(callback: CallbackQuery):
    text = (
        f"📞 <b>Связаться с перевозчиком</b>\n\n"
        f"Телефон: <code>{esc(p['phone'])}</code>\n"
        f"Telegram: {esc(p['telegram'])}\n\n"
        "Нажмите кнопку ниже, чтобы написать в Telegram, или скопируйте номер телефона."
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=contact_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "edit")
async def edit_card(callback: CallbackQuery):
    if not owner(callback.from_user.id):
        await callback.answer("Редактировать визитку может только владелец.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=label, callback_data="field:" + key)] for key, label in PROFILE_FIELDS.items()]
    await callback.message.answer("Выбери, что изменить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("field:"))
async def edit_field(callback: CallbackQuery, state: FSMContext):
    if not owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in PROFILE_FIELDS:
        await callback.answer()
        return
    await state.update_data(key=key)
    await state.set_state(Edit.value)
    current = p.get(key, "")
    await callback.message.answer(
        f"Поле: {PROFILE_FIELDS[key]}\nТекущее значение: {current}\n\n"
        "Отправь новое значение. Чтобы очистить поле, отправь дефис (-)."
    )
    await callback.answer()


@dp.message(Edit.value)
async def save_field(message: Message, state: FSMContext):
    if not owner(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    data = await state.get_data()
    key = data.get("key")
    if key not in PROFILE_FIELDS:
        await state.clear()
        return
    p[key] = "" if message.text.strip() == "-" else message.text.strip()
    save_profile(p)
    await state.clear()
    await message.answer("Данные визитки сохранены.", reply_markup=menu(message.from_user.id))


@dp.callback_query(F.data == "rates")
async def show_rates(callback: CallbackQuery):
    if not owner(callback.from_user.id):
        await callback.answer("Настраивать тарифы может только владелец.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"{label}: {p[key]}", callback_data="rate:" + key)]
               for key, label in RATE_FIELDS.items()]
    await callback.message.answer(
        "Выбери тариф. Все суммы — в рублях (₽):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rate:"))
async def edit_rate(callback: CallbackQuery, state: FSMContext):
    if not owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in RATE_FIELDS:
        await callback.answer()
        return
    await state.update_data(key=key)
    await state.set_state(Rates.value)
    await callback.message.answer(
        f"{RATE_FIELDS[key]}\nСейчас: {p[key]}\nОтправь новое целое число."
    )
    await callback.answer()


@dp.message(Rates.value)
async def save_rate(message: Message, state: FSMContext):
    if not owner(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    data = await state.get_data()
    key = data.get("key")
    try:
        value = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Введи целое число.")
        return
    if key not in RATE_FIELDS or value < 0 or value > 1000000:
        await message.answer("Недопустимое значение.")
        return
    p[key] = value
    save_profile(p)
    await state.clear()
    await message.answer("Тариф сохранён.", reply_markup=menu(message.from_user.id))


async def begin_calc(target, state: FSMContext):
    await state.clear()
    base_address = str(p.get("base_address", "")).strip() or "Москва"
    await state.update_data(start=base_address)
    await state.set_state(Calc.pickup)
    text = (
        "🧮 <b>Расчёт стоимости</b>\n\n"
        "Шаг 1/3. Введи адрес, где нужно забрать груз.\n"
        "Пиши город, улицу и номер дома."
    )
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML")


@dp.callback_query(F.data == "calc")
async def calculate_button(callback: CallbackQuery, state: FSMContext):
    await begin_calc(callback, state)


@dp.message(Calc.pickup)
async def get_pickup(message: Message, state: FSMContext):
    await state.update_data(pickup=message.text.strip())
    await state.set_state(Calc.delivery)
    await message.answer("Шаг 2/3. Введи адрес доставки груза.")


@dp.message(Calc.delivery)
async def get_delivery(message: Message, state: FSMContext):
    await state.update_data(delivery=message.text.strip())
    await state.set_state(Calc.cargo)
    await message.answer(
        "Шаг 3/3. Кратко опиши груз: что перевозим, примерный вес и есть ли грузчики/этажи.\n"
        "Если дополнительных условий нет, напиши «нет»."
    )


@dp.message(Calc.cargo)
async def get_cargo(message: Message, state: FSMContext):
    await state.update_data(cargo=message.text.strip())
    data = await state.get_data()
    await message.answer("⏳ Рассчитываю маршрут. Это может занять несколько секунд.")
    empty_km, empty_minutes, error1 = await route(data["start"], data["pickup"])
    loaded_km, loaded_minutes, error2 = await route(data["pickup"], data["delivery"])
    if empty_km is None or loaded_km is None:
        await state.clear()
        await message.answer(
            "Не удалось рассчитать маршрут автоматически.\n"
            f"Подача: {error1 or 'OK'}\nМаршрут с грузом: {error2 or 'OK'}\n\n"
            "Проверь адреса и попробуй ещё раз. Если ошибка повторяется, свяжись с перевозчиком.",
            reply_markup=menu(message.from_user.id)
        )
        return

    price, empty_cost, loaded_cost = calc_price(empty_km, loaded_km)
    total_km = round(empty_km + loaded_km, 1)
    total_minutes = (empty_minutes or 0) + (loaded_minutes or 0)
    await state.update_data(
        ek=empty_km, et=empty_minutes, lk=loaded_km, lt=loaded_minutes,
        ec=empty_cost, lc=loaded_cost, price=price, total=total_km, minutes=total_minutes
    )
    url = map_url(data["start"], data["pickup"], data["delivery"])
    result_text = (
        "🧾 <b>Предварительный расчёт</b>\n\n"
        f"🚐 {esc(p['vehicle'])}\n"
        f"📍 <b>Подача автомобиля:</b> {esc(data['start'])} → {esc(data['pickup'])}\n"
        f"↳ {empty_km} км · около {empty_minutes} мин · {empty_cost} ₽\n\n"
        f"📦 <b>Перевозка с грузом:</b> {esc(data['pickup'])} → {esc(data['delivery'])}\n"
        f"↳ {loaded_km} км · около {loaded_minutes} мин · {loaded_cost} ₽\n\n"
        f"📏 Общий путь: {total_km} км\n"
        f"⏱️ В пути примерно: {total_minutes} мин, без учёта пробок и погрузки\n"
        f"📦 Груз и условия: {esc(data['cargo'])}\n\n"
        f"💰 <b>Предварительная стоимость: {price} ₽</b>\n\n"
        f"Тариф: подача {p['per_km_empty']} ₽/км + маршрут с грузом {p['per_km_loaded']} ₽/км; "
        f"фиксированная часть {p['base_fee']} ₽; минимум {p['minimum']} ₽.\n"
        "Итоговую цену перевозчик подтвердит после уточнения груза и условий."
    )
    await state.set_state(Calc.result)
    rows = [
        [InlineKeyboardButton(text="🗺 Открыть маршрут на карте", url=url)],
        [InlineKeyboardButton(text="📞 Уточнить заказ", callback_data="contact")],
        [InlineKeyboardButton(text="🔄 Рассчитать другой маршрут", callback_data="calc")]
    ]
    await message.answer(
        result_text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Команды:\n/start — главное меню\n/card — визитка\n/calc — рассчитать стоимость\n\n"
        "Калькулятор показывает предварительную цену. Расстояние и время рассчитываются по картографическому сервису; "
        "пробки, платные дороги, ожидание и дополнительные работы могут изменить итог."
    )


async def main():
    if not TOKEN:
        raise RuntimeError("В файле .env нужно указать BOT_TOKEN от @BotFather.")
    if OWNER_ID == 0:
        log.error("OWNER_TELEGRAM_ID не задан. Админ-функции закрыты до указания личного Telegram ID.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
