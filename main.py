from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from xml.sax.saxutils import escape

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        ConversationHandler,
        MessageHandler,
        filters,
    )
except ImportError:
    ApplicationBuilder = None
    CommandHandler = None
    ContextTypes = None
    ConversationHandler = None
    MessageHandler = None
    ReplyKeyboardMarkup = None
    ReplyKeyboardRemove = None
    Update = None
    filters = None

try:
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer
except ImportError:
    TA_CENTER = None
    A4 = None
    ParagraphStyle = None
    getSampleStyleSheet = None
    mm = None
    pdfmetrics = None
    TTFont = None
    Image = None
    Paragraph = None
    SimpleDocTemplate = None
    Spacer = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CHOICE, AREA, FLOORS, COMPLEXITY, PACKAGE, CONTACT = range(6)

START_KEYBOARD = [["🏠 Рассчитать дом"]]
PACKAGE_KEYBOARD = [["🟢 Эскиз", "🔵 Проект"], ["🟡 Премиум"]]
DESIGN_PRICE_PER_M2_RUB = 2000
COMPLEXITY_MULTIPLIERS = {
    "простой": 1.0,
    "средний": 1.15,
    "сложный": 1.3,
}


@dataclass(slots=True)
class Settings:
    telegram_token: str
    openai_api_key: str
    admin_chat_id: int | None
    openai_text_model: str = "gpt-4.1-mini"
    openai_image_model: str = "gpt-image-1"


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

    if OpenAI is None:
        missing.append("openai")
    if ApplicationBuilder is None:
        missing.append("python-telegram-bot")
    if SimpleDocTemplate is None:
        missing.append("reportlab")

    return missing


def load_settings() -> Settings:
    load_env_file(BASE_DIR / ".env")

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    admin_chat_id_raw = os.getenv("ADMIN_CHAT_ID", "").strip()

    if not telegram_token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавьте его в переменные окружения "
            "или в файл .env рядом с main.py."
        )

    if not openai_api_key:
        raise RuntimeError(
            "Не задан OPENAI_API_KEY. Добавьте его в переменные окружения "
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
        openai_api_key=openai_api_key,
        admin_chat_id=admin_chat_id,
        openai_text_model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        openai_image_model=os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1").strip() or "gpt-image-1",
    )


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


def parse_area(text: str) -> float:
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        raise ValueError("Введите площадь числом, например `150` или `150.5`.")

    value = float(match.group(0).replace(",", "."))
    if value <= 0:
        raise ValueError("Площадь должна быть больше нуля.")

    return value


def parse_floors(text: str) -> int:
    match = re.search(r"\d+", text)
    if not match:
        raise ValueError("Введите количество этажей числом, например `1` или `2`.")

    value = int(match.group(0))
    if value < 1 or value > 10:
        raise ValueError("Количество этажей должно быть в диапазоне от 1 до 10.")

    return value


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


def build_plan_prompt(area: float, floors: int, complexity: str) -> str:
    return (
        "Составь компактное архитектурное предложение для клиента.\n"
        f"Площадь дома: {area:.1f} м2.\n"
        f"Этажность: {floors}.\n"
        f"Сложность проекта: {complexity}.\n"
        "Нужен ответ на русском языке с разделами:\n"
        "1. Общая концепция\n"
        "2. Пример зонирования по этажам\n"
        "3. Конструктивные рекомендации\n"
        "4. На что обратить внимание при проектировании\n"
        "Пиши по делу, без лишнего маркетинга."
    )


def generate_plan_text(client: Any, settings: Settings, area: float, floors: int, complexity: str) -> str:
    prompt = build_plan_prompt(area, floors, complexity)

    if hasattr(client, "responses"):
        try:
            response = client.responses.create(
                model=settings.openai_text_model,
                input=prompt,
            )
            output_text = getattr(response, "output_text", "").strip()
            if output_text:
                return output_text
        except Exception:
            LOGGER.exception("Responses API не сработал, пробую chat.completions.")

    completion = client.chat.completions.create(
        model=settings.openai_text_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты архитектор-концептуалист. Пиши на русском языке коротко, "
                    "структурно и практически полезно."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return (completion.choices[0].message.content or "").strip()


def generate_plan_image(client: Any, settings: Settings, area: float, floors: int) -> bytes:
    image_response = client.images.generate(
        model=settings.openai_image_model,
        prompt=(
            "Architectural floor plan blueprint, clean black and white drawing, "
            f"{area:.1f} square meter house, {floors} floors, technical presentation sheet"
        ),
        size="1024x1024",
    )

    if not getattr(image_response, "data", None):
        raise RuntimeError("OpenAI не вернул изображение.")

    image_item = image_response.data[0]

    image_b64 = getattr(image_item, "b64_json", None)
    if image_b64:
        return base64.b64decode(image_b64)

    image_url = getattr(image_item, "url", None)
    if image_url:
        with urlopen(image_url, timeout=30) as response:
            return response.read()

    raise RuntimeError("Не удалось получить данные изображения от OpenAI.")


def create_pdf(data: dict[str, Any], image_bytes: bytes | None, filename: Path) -> None:
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
        Paragraph("Архитектурное предложение", title_style),
        Spacer(1, 8),
        Paragraph(f"Площадь: {paragraph_text(data['area'])} м2", body_style),
        Paragraph(f"Этажи: {paragraph_text(data['floors'])}", body_style),
        Paragraph(f"Сложность: {paragraph_text(data['complexity'])}", body_style),
        Paragraph(
            f"Базовая ставка проектирования: {paragraph_text(format_rubles(DESIGN_PRICE_PER_M2_RUB))}/м²",
            body_style,
        ),
        Spacer(1, 8),
        Paragraph("Концепция планировки", title_style),
        Paragraph(paragraph_text(data["plan_text"]), body_style),
    ]

    if image_bytes:
        image_stream = BytesIO(image_bytes)
        image_stream.seek(0)
        story.extend(
            [
                Spacer(1, 8),
                Image(image_stream, width=160 * mm, height=100 * mm),
            ]
        )

    if data.get("price") is not None:
        story.extend(
            [
                Spacer(1, 12),
                Paragraph(
                    f"Полная стоимость проектирования: {paragraph_text(format_rubles(data['price']))}",
                    title_style,
                ),
                Paragraph(
                    f"Ставка проектирования: {paragraph_text(format_rubles(data['price_per_m2']))}/м²",
                    body_style,
                ),
            ]
        )

    if data.get("final_price") is not None:
        story.extend(
            [
                Spacer(1, 8),
                Paragraph(
                    f"Стоимость пакета {paragraph_text(data['package'])}: "
                    f"{paragraph_text(format_rubles(data['final_price']))}",
                    title_style,
                ),
                Paragraph(
                    f"Ставка пакета: {paragraph_text(format_rubles(data['package_price_per_m2']))}/м²",
                    body_style,
                ),
            ]
        )

    if data.get("timeline"):
        story.append(Paragraph(f"Срок: {paragraph_text(data['timeline'])}", body_style))

    story.extend(
        [
            Paragraph(
                "Точная стоимость проектирования зависит от состава проекта, этажности "
                "и конструктивных решений.",
                body_style,
            ),
            Spacer(1, 12),
            Paragraph("Свяжитесь со мной для разработки полноценного проекта.", body_style),
        ]
    )

    doc.build(story)

# ===== ЛОГИКА БОТА =====

def build_lead_message(user: Any, data: dict[str, Any], contact: str) -> str:
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "Без имени"
    username = f"@{user.username}" if getattr(user, "username", None) else "не указан"
    final_price = data.get("final_price")
    total_price = data.get("price")
    display_price = final_price if final_price is not None else total_price
    package = data.get("package", "не выбран")

    return (
        "Новая заявка\n"
        f"Пользователь: {full_name}\n"
        f"Username: {username}\n"
        f"User ID: {user.id}\n"
        f"Контакт: {contact}\n"
        f"Площадь: {data['area']} м2\n"
        f"Этажи: {data['floors']}\n"
        f"Сложность: {data['complexity']}\n"
        f"Пакет: {package}\n"
        f"Стоимость проектирования: {format_rubles(display_price)}"
    )


async def start(update: Any, context: Any) -> int:
    if update.message is None:
        return CHOICE

    await update.message.reply_text(
        "Привет. Рассчитаю стоимость проектирования и подготовлю концепцию планировки.",
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
    floors_value = context.user_data["floors"]
    complexity_value = context.user_data["complexity"]
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
        f"Срок: {context.user_data['timeline']}\n"
        "Генерирую план..."
    )

    settings: Settings = context.application.bot_data["settings"]
    client = context.application.bot_data["openai_client"]

    try:
        text_plan = generate_plan_text(client, settings, area_value, floors_value, complexity_value)
    except Exception:
        LOGGER.exception("Ошибка генерации текстовой концепции")
        text_plan = (
            "Не удалось получить текстовую концепцию от OpenAI. "
            "Проверьте API-ключ и доступность модели."
        )

    image_bytes: bytes | None = None
    try:
        image_bytes = generate_plan_image(client, settings, area_value, floors_value)
    except Exception:
        LOGGER.exception("Ошибка генерации изображения")

    context.user_data["plan_text"] = text_plan

    if image_bytes:
        image_stream = BytesIO(image_bytes)
        image_stream.name = "plan.png"
        await update.message.reply_photo(photo=image_stream)

    await update.message.reply_text(text_plan[:3500] or "Описание планировки пустое.")

    pdf_path = BASE_DIR / f"offer_{update.effective_user.id}.pdf"
    pdf_data = {
        "area": area_value,
        "floors": floors_value,
        "complexity": complexity_value,
        "price": context.user_data["price"],
        "price_per_m2": context.user_data["price_per_m2"],
        "final_price": final_price,
        "package_price_per_m2": package_price_per_m2,
        "package": package_name,
        "timeline": context.user_data["timeline"],
        "plan_text": text_plan,
    }

    try:
        create_pdf(pdf_data, image_bytes, pdf_path)
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
        "price": context.user_data.get("price"),
        "final_price": context.user_data.get("final_price"),
        "package": context.user_data.get("package"),
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
        await update.message.reply_text("Диалог отменён.", reply_markup=ReplyKeyboardRemove())

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
    application.bot_data["openai_client"] = OpenAI(api_key=settings.openai_api_key)

    conversation_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choice)],
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, area)],
            FLOORS: [MessageHandler(filters.TEXT & ~filters.COMMAND, floors)],
            COMPLEXITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, complexity)],
            PACKAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, package_choice)],
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
        application = build_application(settings)
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}")
        return 1

    print("Бот запущен...")
    application.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
