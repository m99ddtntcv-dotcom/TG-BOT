import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

from openai import OpenAI

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm

# ===== НАСТРОЙКИ =====
TOKEN = "ТВОЙ_TELEGRAM_TOKEN"
OPENAI_KEY = "ТВОЙ_OPENAI_API_KEY"
ADMIN_ID = 123456789

client = OpenAI(api_key=OPENAI_KEY)

CHOICE, AREA, FLOORS, COMPLEXITY, PACKAGE, CONTACT = range(6)

# ===== PDF =====
def create_pdf(data, image_url, filename="offer.pdf"):
    pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))

    doc = SimpleDocTemplate(filename, pagesize=A4)

    primary = colors.HexColor("#111111")
    accent = colors.HexColor("#C8A96A")

    title = ParagraphStyle(name="title", fontName="DejaVu", fontSize=18, alignment=TA_CENTER)
    section = ParagraphStyle(name="section", fontName="DejaVu", fontSize=13, textColor=accent)
    text = ParagraphStyle(name="text", fontName="DejaVu", fontSize=10)

    story = []

    # логотип
    try:
        story.append(Image("logo.png", width=60*mm, height=20*mm))
    except:
        pass

    story.append(Spacer(1, 10))

    story.append(Paragraph("Коммерческое предложение", title))
    story.append(Spacer(1, 10))

    # параметры
    story.append(Paragraph("Параметры проекта", section))

    table_data = [
        ["Площадь", f"{data['area']} м²"],
        ["Этажи", f"{data['floors']}"],
        ["Сложность", f"{data['complexity']}"]
    ]

    table = Table(table_data)
    table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "DejaVu")]))

    story.append(table)
    story.append(Spacer(1, 15))

    # планировка
    story.append(Paragraph("Концепция планировки", section))
    story.append(Paragraph(data["plan_text"], text))

    story.append(Spacer(1, 10))

    # изображение
    try:
        img_data = requests.get(image_url).content
        with open("plan.png", "wb") as f:
            f.write(img_data)
        story.append(Image("plan.png", width=170*mm, height=100*mm))
    except:
        pass

    story.append(Spacer(1, 15))

    # стоимость
    story.append(Paragraph("Стоимость", section))
    story.append(Paragraph(f"{data['final_price']:,} ₽", title))

    # разбивка
    story.append(Paragraph("Структура стоимости", section))
    table_data = [
        ["АР", f"{data['ar']:,} ₽"],
        ["КР", f"{data['kr']:,} ₽"],
        ["ИЖС", f"{data['eng']:,} ₽"],
    ]
    story.append(Table(table_data))

    # пакет
    story.append(Spacer(1, 10))
    story.append(Paragraph("Пакет", section))
    story.append(Paragraph(data["package"], text))

    # сроки
    story.append(Spacer(1, 10))
    story.append(Paragraph("Сроки разработки", section))
    story.append(Paragraph(data["timeline"], text))

    story.append(Spacer(1, 20))
    story.append(Paragraph("Свяжитесь со мной для разработки проекта", text))

    doc.build(story)

# ===== ЛОГИКА БОТА =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["🏠 Рассчитать дом"]]
    await update.message.reply_text(
        "Привет! Рассчитаю стоимость и создам планировку",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return CHOICE

async def choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите площадь дома (м²):")
    return AREA

async def area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["area"] = update.message.text
    await update.message.reply_text("Сколько этажей?")
    return FLOORS

async def floors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["floors"] = update.message.text
    await update.message.reply_text("Сложность (простой / средний / сложный)?")
    return COMPLEXITY

async def complexity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["complexity"] = update.message.text

    try:
        area = float(context.user_data["area"])
        floors = int(context.user_data["floors"])
    except:
        area, floors = 150, 1

    base_price = 2000
    floor_coef = 1 + (floors - 1) * 0.1

    comp = context.user_data["complexity"].lower()
    if "сред" in comp:
        complexity_coef = 1.15
    elif "слож" in comp:
        complexity_coef = 1.3
    else:
        complexity_coef = 1

    price = int(area * base_price * floor_coef * complexity_coef)

    # разбивка
    ar = int(price * 0.4)
    kr = int(price * 0.35)
    eng = int(price * 0.25)

    context.user_data.update({
        "price": price,
        "ar": ar,
        "kr": kr,
        "eng": eng
    })

    await update.message.reply_text(
        f"💰 {price:,} ₽\n"
        f"АР: {ar:,} ₽\nКР: {kr:,} ₽\nИЖС: {eng:,} ₽"
    )

    keyboard = [["🟢 Эскиз", "🔵 Проект"], ["🟡 Премиум"]]
    await update.message.reply_text(
        "Выберите пакет:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

    return PACKAGE

async def package_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    ar, kr, eng = context.user_data["ar"], context.user_data["kr"], context.user_data["eng"]

    if "Эскиз" in choice:
        final_price = ar
        desc = "Эскиз"
        min_d, max_d = 5, 10
    elif "Проект" in choice:
        final_price = ar + kr
        desc = "Проект"
        min_d, max_d = 15, 25
    else:
        final_price = ar + kr + eng
        desc = "Премиум"
        min_d, max_d = 25, 40

    area = float(context.user_data["area"])
    coef = 1.2 if area > 150 else 1
    min_d, max_d = int(min_d * coef), int(max_d * coef)

    context.user_data.update({
        "final_price": final_price,
        "package": desc,
        "timeline": f"{min_d}-{max_d} дней"
    })

    await update.message.reply_text("Генерирую план...")

    # AI
    text_plan = client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": f"Планировка дома {area} м2"}]
    ).choices[0].message.content

    img = client.images.generate(
        model="gpt-image-1",
        prompt=f"floor plan {area} sqm house"
    )

    image_url = img.data[0].url

    await update.message.reply_photo(photo=image_url)
    await update.message.reply_text(text_plan[:1000])

    data = {
        "area": area,
        "floors": context.user_data["floors"],
        "complexity": context.user_data["complexity"],
        "plan_text": text_plan,
        "final_price": final_price,
        "ar": ar,
        "kr": kr,
        "eng": eng,
        "package": desc,
        "timeline": context.user_data["timeline"]
    }

    create_pdf(data, image_url)

    await update.message.reply_document(open("offer.pdf", "rb"))
    await update.message.reply_text("Оставьте контакт:")

    return CONTACT

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"Заявка: {update.message.text}"
    )
    await update.message.reply_text("Спасибо! Свяжусь с вами.")
    return ConversationHandler.END

# ===== ЗАПУСК =====

app = ApplicationBuilder().token(TOKEN).build()

conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        CHOICE: [MessageHandler(filters.TEXT, choice)],
        AREA: [MessageHandler(filters.TEXT, area)],
        FLOORS: [MessageHandler(filters.TEXT, floors)],
        COMPLEXITY: [MessageHandler(filters.TEXT, complexity)],
        PACKAGE: [MessageHandler(filters.TEXT, package_choice)],
        CONTACT: [MessageHandler(filters.TEXT, contact)],
    },
    fallbacks=[]
)

app.add_handler(conv)

print("Бот запущен 🚀")
app.run_polling()