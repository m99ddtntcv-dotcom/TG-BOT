from __future__ import annotations

import fcntl
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

try:
    from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
    from telegram.error import Conflict as TelegramConflictError
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ConversationHandler,
        MessageHandler,
        filters,
    )
except ImportError:
    ApplicationBuilder = None
    CommandHandler = None
    ConversationHandler = None
    MessageHandler = None
    ReplyKeyboardMarkup = None
    ReplyKeyboardRemove = None
    TelegramConflictError = None
    filters = None

try:
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
except ImportError:
    TA_CENTER = None
    A4 = None
    ParagraphStyle = None
    getSampleStyleSheet = None
    mm = None
    pdfmetrics = None
    TTFont = None
    Paragraph = None
    SimpleDocTemplate = None
    Spacer = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LOCK_FILE = BASE_DIR / ".bot.lock"
(
    CHOICE,
    AREA,
    FLOORS,
    COMPLEXITY,
    PACKAGE,
    BEDROOMS,
    BATHROOMS,
    KITCHEN_LIVING,
    CABINET,
    WARDROBE,
    UTILITY,
    GUEST_ROOM,
    TERRACE,
    GARAGE,
    SAUNA,
    MASTER_BEDROOM,
    EXTRA_ROOMS,
    CONTACT,
) = range(18)

RUN_LOCK_HANDLE: Any = None

START_KEYBOARD = [["🏠 Рассчитать дом"]]
PACKAGE_KEYBOARD = [["🟢 Эскиз", "🔵 Проект"], ["🟡 Премиум"]]
YES_NO_KEYBOARD = [["Да", "Нет"]]
DESIGN_PRICE_PER_M2_RUB = 2000
COMPLEXITY_MULTIPLIERS = {
    "простой": 1.0,
    "средний": 1.15,
    "сложный": 1.3,
}


@dataclass(slots=True)
class Settings:
    telegram_token: str
    admin_chat_id: int | None


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_missing_dependencies() -> list[str]:
    missing: list[str] = []

    if ApplicationBuilder is None:
        missing.append("python-telegram-bot")
    if SimpleDocTemplate is None:
        missing.append("reportlab")

    return missing


def load_settings() -> Settings:
    load_env_file(BASE_DIR / ".env")

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    admin_chat_id_raw = os.getenv("ADMIN_CHAT_ID", "").strip()

    if not telegram_token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавьте его в переменные окружения "
            "или в файл .env рядом с main.py."
        )

    admin_chat_id: int | None = None
    if admin_chat_id_raw:
        try:
            admin_chat_id = int(admin_chat_id_raw)
        except ValueError as exc:
            raise RuntimeError("ADMIN_CHAT_ID должен быть числом, например 123456789.") from exc

    return Settings(
        telegram_token=telegram_token,
        admin_chat_id=admin_chat_id,
    )


def acquire_instance_lock() -> None:
    global RUN_LOCK_HANDLE

    lock_handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock_handle.close()
        raise RuntimeError(
            "Бот уже запущен в другом локальном процессе. "
            "Остановите предыдущий запуск и попробуйте снова."
        ) from exc

    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    RUN_LOCK_HANDLE = lock_handle


def find_pdf_font() -> str:
    candidates = [
        ("DejaVu", BASE_DIR / "DejaVuSans.ttf"),
        ("DejaVu", BASE_DIR / "DejaVu Sans" / "DejaVuSans.ttf"),
        ("DejaVu", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")),
        ("Arial", Path("/System/Library/Fonts/Supplemental/Arial.ttf")),
        ("Arial", Path("/Library/Fonts/Arial.ttf")),
        ("ArialUnicode", Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")),
        ("ArialUnicode", Path("/Library/Fonts/Arial Unicode.ttf")),
    ]

    registered_fonts = set(pdfmetrics.getRegisteredFontNames()) if pdfmetrics else set()

    for font_name, font_path in candidates:
        if not font_path.exists():
            continue

        try:
            if font_name not in registered_fonts:
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
            return font_name
        except Exception:
            LOGGER.exception("Не удалось зарегистрировать шрифт %s", font_path)

    raise RuntimeError(
        "Не найден шрифт с поддержкой кириллицы для PDF. "
        "Положите DejaVuSans.ttf рядом с main.py, в папку `DejaVu Sans`, "
        "или установите Arial/DejaVu в систему."
    )


def paragraph_text(value: Any) -> str:
    return escape(str(value)).replace("\n", "<br/>")


def format_rubles(value: int | float) -> str:
    return f"{int(value):,}".replace(",", " ") + " ₽"


def format_yes_no(value: bool) -> str:
    return "Да" if value else "Нет"


def parse_area(text: str) -> float:
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        raise ValueError("Введите площадь числом, например `150` или `150.5`.")

    value = float(match.group(0).replace(",", "."))
    if value <= 0:
        raise ValueError("Площадь должна быть больше нуля.")

    return value


def parse_integer_value(text: str, label: str, minimum: int = 0, maximum: int = 20) -> int:
    match = re.search(r"\d+", text)
    if not match:
        raise ValueError(f"Введите {label} числом.")

    value = int(match.group(0))
    if value < minimum or value > maximum:
        raise ValueError(f"{label.capitalize()} должно быть в диапазоне от {minimum} до {maximum}.")

    return value


def parse_floors(text: str) -> int:
    return parse_integer_value(text, "количество этажей", minimum=1, maximum=10)


def parse_yes_no(text: str) -> bool:
    cleaned = text.strip().lower()
    yes_values = ("да", "нуж", "yes", "y", "1")
    no_values = ("нет", "не", "no", "n", "0")

    if cleaned.startswith(yes_values):
        return True
    if cleaned.startswith(no_values):
        return False

    raise ValueError("Ответьте `Да` или `Нет`.")


def normalize_complexity(text: str) -> str:
    cleaned = text.strip().lower()
    mapping = {
        "простой": "простой",
        "прост": "простой",
        "средний": "средний",
        "средн": "средний",
        "сложный": "сложный",
        "сложн": "сложный",
    }

    for key, value in mapping.items():
        if cleaned.startswith(key):
            return value

    raise ValueError("Укажите сложность: простой, средний или сложный.")


def calculate_price(area: float, floors: int, complexity: str) -> int:
    floor_multiplier = 1 + max(floors - 1, 0) * 0.1
    complexity_multiplier = COMPLEXITY_MULTIPLIERS[complexity]
    return round(area * DESIGN_PRICE_PER_M2_RUB * floor_multiplier * complexity_multiplier)


def normalize_extra_rooms(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return "Нет дополнительных помещений."

    lowered = cleaned.lower()
    if lowered in {"нет", "не нужны", "не нужно", "-", "без дополнительных помещений"}:
        return "Нет дополнительных помещений."

    return cleaned


def build_rooms_summary(rooms: dict[str, Any]) -> str:
    return (
        "Состав помещений:\n"
        f"- Спальни: {rooms['bedrooms']}\n"
        f"- Санузлы: {rooms['bathrooms']}\n"
        f"- Кухня-гостиная: {format_yes_no(rooms['kitchen_living'])}\n"
        f"- Кабинет: {format_yes_no(rooms['cabinet'])}\n"
        f"- Гардеробная: {format_yes_no(rooms['wardrobe'])}\n"
        f"- Котельная / постирочная: {format_yes_no(rooms['utility'])}\n"
        f"- Гостевая спальня: {format_yes_no(rooms['guest_room'])}\n"
        f"- Терраса: {format_yes_no(rooms['terrace'])}\n"
        f"- Гараж: {format_yes_no(rooms['garage'])}\n"
        f"- Сауна: {format_yes_no(rooms['sauna'])}\n"
        f"- Мастер-спальня: {format_yes_no(rooms['master_bedroom'])}\n"
        f"- Дополнительно: {rooms['extra_rooms']}"
    )


def build_brief_summary(data: dict[str, Any]) -> str:
    return (
        f"Пакет: {data['package']}\n"
        f"Стоимость: {format_rubles(data['final_price'])} ({format_rubles(data['package_price_per_m2'])}/м²)\n"
        f"Срок: {data['timeline']}\n\n"
        f"{data['rooms_summary']}"
    )


def create_pdf(data: dict[str, Any], filename: Path) -> None:
    font_name = find_pdf_font()
    doc = SimpleDocTemplate(
        str(filename),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        name="title",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=18,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    body_style = ParagraphStyle(
        name="body",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=11,
        leading=14,
        spaceAfter=10,
    )

    story = [
        Paragraph("Анкета на проектирование", title_style),
        Spacer(1, 8),
        Paragraph(f"Площадь: {paragraph_text(data['area'])} м2", body_style),
        Paragraph(f"Этажи: {paragraph_text(data['floors'])}", body_style),
        Paragraph(f"Сложность: {paragraph_text(data['complexity'])}", body_style),
        Paragraph(
            f"Базовая ставка проектирования: {paragraph_text(format_rubles(DESIGN_PRICE_PER_M2_RUB))}/м²",
            body_style,
        ),
        Spacer(1, 8),
        Paragraph("Параметры проекта", title_style),
        Paragraph(
            paragraph_text(
                f"Полная стоимость проектирования: {format_rubles(data['price'])}\n"
                f"Ставка проектирования: {format_rubles(data['price_per_m2'])}/м²\n"
                f"Пакет: {data['package']}\n"
                f"Стоимость пакета: {format_rubles(data['final_price'])}\n"
                f"Ставка пакета: {format_rubles(data['package_price_per_m2'])}/м²\n"
                f"Срок: {data['timeline']}"
            ),
            body_style,
        ),
        Spacer(1, 8),
        Paragraph("Пожелания по помещениям", title_style),
        Paragraph(paragraph_text(data["rooms_summary"]), body_style),
        Spacer(1, 12),
        Paragraph(
            "Анкета фиксирует базовые пожелания клиента по составу помещений и помогает "
            "подготовить задание на проектирование.",
            body_style,
        ),
    ]

    doc.build(story)


def build_lead_message(user: Any, data: dict[str, Any], contact: str) -> str:
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "Без имени"
    username = f"@{user.username}" if getattr(user, "username", None) else "не указан"

    return (
        "Новая заявка\n"
        f"Пользователь: {full_name}\n"
        f"Username: {username}\n"
        f"User ID: {user.id}\n"
        f"Контакт: {contact}\n"
        f"Площадь: {data['area']} м2\n"
        f"Этажи: {data['floors']}\n"
        f"Сложность: {data['complexity']}\n"
        f"Пакет: {data['package']}\n"
        f"Стоимость проектирования: {format_rubles(data['final_price'])}\n"
        f"Срок: {data['timeline']}\n\n"
        f"{data['rooms_summary']}"
    )


async def start(update: Any, context: Any) -> int:
    if update.message is None:
        return CHOICE

    await update.message.reply_text(
        "Привет. Рассчитаю стоимость проектирования и соберу опрос по помещениям.",
        reply_markup=ReplyKeyboardMarkup(START_KEYBOARD, resize_keyboard=True),
    )
    return CHOICE


async def choice(update: Any, context: Any) -> int:
    if update.message is None:
        return CHOICE

    user_choice = update.message.text.strip().lower()
    if "рассчитать" not in user_choice:
        await update.message.reply_text("Нажмите кнопку `🏠 Рассчитать дом`, чтобы начать.")
        return CHOICE

    await update.message.reply_text("Введите площадь дома в м²:")
    return AREA


async def area(update: Any, context: Any) -> int:
    if update.message is None:
        return AREA

    try:
        area_value = parse_area(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return AREA

    context.user_data["area"] = area_value
    await update.message.reply_text("Сколько этажей?")
    return FLOORS


async def floors(update: Any, context: Any) -> int:
    if update.message is None:
        return FLOORS

    try:
        floors_value = parse_floors(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return FLOORS

    context.user_data["floors"] = floors_value
    await update.message.reply_text("Укажите сложность: простой, средний или сложный.")
    return COMPLEXITY


async def complexity(update: Any, context: Any) -> int:
    if update.message is None:
        return COMPLEXITY

    try:
        complexity_value = normalize_complexity(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return COMPLEXITY

    area_value = context.user_data["area"]
    floors_value = context.user_data["floors"]
    total_price = calculate_price(area_value, floors_value, complexity_value)
    price_per_m2 = round(total_price / area_value)
    ar = int(total_price * 0.4)
    kr = int(total_price * 0.35)
    eng = total_price - ar - kr

    context.user_data.update(
        {
            "complexity": complexity_value,
            "price": total_price,
            "price_per_m2": price_per_m2,
            "ar": ar,
            "kr": kr,
            "eng": eng,
        }
    )

    await update.message.reply_text(
        "Стоимость проектирования:\n"
        f"Полный комплект: {format_rubles(total_price)}\n"
        f"Ставка: {format_rubles(price_per_m2)}/м²\n"
        f"АР: {format_rubles(ar)}\n"
        f"КР: {format_rubles(kr)}\n"
        f"Инженерия: {format_rubles(eng)}"
    )

    await update.message.reply_text(
        "Выберите пакет:",
        reply_markup=ReplyKeyboardMarkup(PACKAGE_KEYBOARD, resize_keyboard=True),
    )
    return PACKAGE


async def package_choice(update: Any, context: Any) -> int:
    if update.message is None:
        return PACKAGE

    selection = update.message.text.strip()
    ar = context.user_data["ar"]
    kr = context.user_data["kr"]
    eng = context.user_data["eng"]

    if "Эскиз" in selection:
        final_price = ar
        package_name = "Эскиз"
        min_days, max_days = 5, 10
    elif "Проект" in selection:
        final_price = ar + kr
        package_name = "Проект"
        min_days, max_days = 15, 25
    elif "Премиум" in selection:
        final_price = ar + kr + eng
        package_name = "Премиум"
        min_days, max_days = 25, 40
    else:
        await update.message.reply_text("Выберите пакет кнопкой ниже.")
        return PACKAGE

    area_value = context.user_data["area"]
    duration_multiplier = 1.2 if area_value > 150 else 1.0
    min_days = int(min_days * duration_multiplier)
    max_days = int(max_days * duration_multiplier)
    package_price_per_m2 = round(final_price / area_value)

    context.user_data.update(
        {
            "final_price": final_price,
            "package": package_name,
            "timeline": f"{min_days}-{max_days} дней",
            "package_price_per_m2": package_price_per_m2,
        }
    )

    await update.message.reply_text(
        f"Пакет {package_name}: {format_rubles(final_price)} "
        f"({format_rubles(package_price_per_m2)}/м²)\n"
        f"Срок: {context.user_data['timeline']}"
    )
    await update.message.reply_text("Теперь короткий опрос по помещениям. Сколько спален нужно?")
    return BEDROOMS


async def bedrooms(update: Any, context: Any) -> int:
    if update.message is None:
        return BEDROOMS

    try:
        bedrooms_value = parse_integer_value(update.message.text, "количество спален", minimum=1, maximum=20)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return BEDROOMS

    context.user_data["bedrooms"] = bedrooms_value
    await update.message.reply_text("Сколько санузлов нужно?")
    return BATHROOMS


async def bathrooms(update: Any, context: Any) -> int:
    if update.message is None:
        return BATHROOMS

    try:
        bathrooms_value = parse_integer_value(update.message.text, "количество санузлов", minimum=1, maximum=20)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return BATHROOMS

    context.user_data["bathrooms"] = bathrooms_value
    await update.message.reply_text(
        "Нужна кухня-гостиная?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return KITCHEN_LIVING


async def kitchen_living(update: Any, context: Any) -> int:
    if update.message is None:
        return KITCHEN_LIVING

    try:
        context.user_data["kitchen_living"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return KITCHEN_LIVING

    await update.message.reply_text(
        "Нужен кабинет?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return CABINET


async def cabinet(update: Any, context: Any) -> int:
    if update.message is None:
        return CABINET

    try:
        context.user_data["cabinet"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return CABINET

    await update.message.reply_text(
        "Нужна гардеробная?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return WARDROBE


async def wardrobe(update: Any, context: Any) -> int:
    if update.message is None:
        return WARDROBE

    try:
        context.user_data["wardrobe"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return WARDROBE

    await update.message.reply_text(
        "Нужны котельная или постирочная?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return UTILITY


async def utility(update: Any, context: Any) -> int:
    if update.message is None:
        return UTILITY

    try:
        context.user_data["utility"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return UTILITY

    await update.message.reply_text(
        "Нужна гостевая спальня?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return GUEST_ROOM


async def guest_room(update: Any, context: Any) -> int:
    if update.message is None:
        return GUEST_ROOM

    try:
        context.user_data["guest_room"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return GUEST_ROOM

    await update.message.reply_text(
        "Нужна терраса?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return TERRACE


async def terrace(update: Any, context: Any) -> int:
    if update.message is None:
        return TERRACE

    try:
        context.user_data["terrace"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return TERRACE

    await update.message.reply_text(
        "Нужен гараж?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return GARAGE


async def garage(update: Any, context: Any) -> int:
    if update.message is None:
        return GARAGE

    try:
        context.user_data["garage"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return GARAGE

    await update.message.reply_text(
        "Нужна сауна?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return SAUNA


async def sauna(update: Any, context: Any) -> int:
    if update.message is None:
        return SAUNA

    try:
        context.user_data["sauna"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return SAUNA

    await update.message.reply_text(
        "Нужна мастер-спальня?",
        reply_markup=ReplyKeyboardMarkup(YES_NO_KEYBOARD, resize_keyboard=True),
    )
    return MASTER_BEDROOM


async def master_bedroom(update: Any, context: Any) -> int:
    if update.message is None:
        return MASTER_BEDROOM

    try:
        context.user_data["master_bedroom"] = parse_yes_no(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return MASTER_BEDROOM

    await update.message.reply_text(
        "Какие еще помещения нужны? Напишите через запятую или `нет`.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EXTRA_ROOMS


async def extra_rooms(update: Any, context: Any) -> int:
    if update.message is None:
        return EXTRA_ROOMS

    rooms = {
        "bedrooms": context.user_data["bedrooms"],
        "bathrooms": context.user_data["bathrooms"],
        "kitchen_living": context.user_data["kitchen_living"],
        "cabinet": context.user_data["cabinet"],
        "wardrobe": context.user_data["wardrobe"],
        "utility": context.user_data["utility"],
        "guest_room": context.user_data["guest_room"],
        "terrace": context.user_data["terrace"],
        "garage": context.user_data["garage"],
        "sauna": context.user_data["sauna"],
        "master_bedroom": context.user_data["master_bedroom"],
        "extra_rooms": normalize_extra_rooms(update.message.text),
    }
    rooms_summary = build_rooms_summary(rooms)

    context.user_data.update(
        {
            "rooms": rooms,
            "rooms_summary": rooms_summary,
        }
    )

    brief_data = {
        "package": context.user_data["package"],
        "final_price": context.user_data["final_price"],
        "package_price_per_m2": context.user_data["package_price_per_m2"],
        "timeline": context.user_data["timeline"],
        "rooms_summary": rooms_summary,
    }

    await update.message.reply_text(build_brief_summary(brief_data))

    pdf_path = BASE_DIR / f"offer_{update.effective_user.id}.pdf"
    pdf_data = {
        "area": context.user_data["area"],
        "floors": context.user_data["floors"],
        "complexity": context.user_data["complexity"],
        "price": context.user_data["price"],
        "price_per_m2": context.user_data["price_per_m2"],
        "final_price": context.user_data["final_price"],
        "package_price_per_m2": context.user_data["package_price_per_m2"],
        "package": context.user_data["package"],
        "timeline": context.user_data["timeline"],
        "rooms_summary": rooms_summary,
    }

    try:
        create_pdf(pdf_data, pdf_path)
        with pdf_path.open("rb") as pdf_file:
            await update.message.reply_document(document=pdf_file, filename="offer.pdf")
    except Exception:
        LOGGER.exception("Ошибка создания PDF")
        await update.message.reply_text("PDF не удалось сформировать. Проверьте шрифт и библиотеку reportlab.")
    finally:
        if pdf_path.exists():
            pdf_path.unlink()

    await update.message.reply_text("Оставьте ваш контакт для связи.")
    return CONTACT


async def contact(update: Any, context: Any) -> int:
    if update.message is None:
        return ConversationHandler.END

    contact_value = update.message.text.strip()
    settings: Settings = context.application.bot_data["settings"]

    lead_data = {
        "area": context.user_data.get("area"),
        "floors": context.user_data.get("floors"),
        "complexity": context.user_data.get("complexity"),
        "final_price": context.user_data.get("final_price"),
        "package": context.user_data.get("package"),
        "timeline": context.user_data.get("timeline"),
        "rooms_summary": context.user_data.get("rooms_summary"),
    }

    lead_message = build_lead_message(update.effective_user, lead_data, contact_value)

    if settings.admin_chat_id is not None:
        await context.bot.send_message(chat_id=settings.admin_chat_id, text=lead_message)
        reply_text = "Спасибо. Заявка отправлена."
    else:
        LOGGER.warning("ADMIN_CHAT_ID не задан. Заявка не была отправлена администратору.")
        LOGGER.info(lead_message)
        reply_text = "Спасибо. Контакт получен, но ADMIN_CHAT_ID пока не настроен."

    context.user_data.clear()
    await update.message.reply_text(reply_text, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cancel(update: Any, context: Any) -> int:
    context.user_data.clear()

    if update.message is not None:
        await update.message.reply_text("Диалог отменен.", reply_markup=ReplyKeyboardRemove())

    return ConversationHandler.END


def build_application(settings: Settings) -> Any:
    missing_dependencies = get_missing_dependencies()
    if missing_dependencies:
        package_list = ", ".join(missing_dependencies)
        raise RuntimeError(
            f"Не хватает зависимостей: {package_list}. "
            "Установите их командой `pip install -r requirements.txt`."
        )

    application = ApplicationBuilder().token(settings.telegram_token).build()
    application.bot_data["settings"] = settings

    conversation_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choice)],
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, area)],
            FLOORS: [MessageHandler(filters.TEXT & ~filters.COMMAND, floors)],
            COMPLEXITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, complexity)],
            PACKAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, package_choice)],
            BEDROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bedrooms)],
            BATHROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bathrooms)],
            KITCHEN_LIVING: [MessageHandler(filters.TEXT & ~filters.COMMAND, kitchen_living)],
            CABINET: [MessageHandler(filters.TEXT & ~filters.COMMAND, cabinet)],
            WARDROBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wardrobe)],
            UTILITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, utility)],
            GUEST_ROOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, guest_room)],
            TERRACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, terrace)],
            GARAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, garage)],
            SAUNA: [MessageHandler(filters.TEXT & ~filters.COMMAND, sauna)],
            MASTER_BEDROOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, master_bedroom)],
            EXTRA_ROOMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, extra_rooms)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conversation_handler)
    return application


def main() -> int:
    try:
        missing_dependencies = get_missing_dependencies()
        if missing_dependencies:
            package_list = ", ".join(missing_dependencies)
            raise RuntimeError(
                f"Не хватает зависимостей: {package_list}. "
                "Установите их командой `pip install -r requirements.txt`."
            )

        settings = load_settings()
        acquire_instance_lock()
        application = build_application(settings)
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}")
        return 1

    print("Бот запущен...")
    try:
        application.run_polling()
    except Exception as exc:
        if TelegramConflictError is not None and isinstance(exc, TelegramConflictError):
            print(
                "Ошибка запуска: этот токен уже используется другим экземпляром бота "
                "через getUpdates. Остановите второй запуск бота "
                "(например локальный процесс, Render worker или другой сервер) "
                "и попробуйте снова."
            )
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
