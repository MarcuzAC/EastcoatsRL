import logging
import io
import qrcode
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from fastapi import FastAPI, Request
import asyncio
import os
import config
from database import Session, User, Product, Order, Payment
from monero_handler import MoneroHandler

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI()

# Telegram Bot setup
WEBHOOK_URL = f"{os.environ.get('RENDER_EXTERNAL_URL', 'https://your-app.onrender.com')}/webhook/{config.BOT_TOKEN}"

class MoneroBot:
    def __init__(self):
        self.application = Application.builder().token(config.BOT_TOKEN).build()
        self.monero = MoneroHandler()
        self.setup_handlers()

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("products", self.show_products))
        self.application.add_handler(CommandHandler("orders", self.show_orders))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await self._register_user(user)
        welcome_text = (
            "🤖 **Welcome to Crypto Store Bot!**\n\n"
            "🛍️ **Browse Products**: Use /products to see available items\n"
            "💳 **Pay with Monero**: Secure and private cryptocurrency payments\n"
            "📦 **Instant Delivery**: Digital products delivered automatically\n\n"
            "**Commands:**\n/products - Browse available products\n/orders - View your orders\n\n"
            "Start shopping now! 🎉"
        )
        keyboard = [
            [InlineKeyboardButton("🛍️ Browse Products", callback_data="show_products")],
            [InlineKeyboardButton("📋 My Orders", callback_data="my_orders")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with Session() as session:
            products = session.query(Product).filter(Product.is_available == True).all()
            if not products:
                await update.message.reply_text("❌ No products available at the moment.")
                return
            text = "🛍️ **Available Products:**\n\n"
            keyboard = []
            for product in products:
                text += f"**{product.name}**\n💰 {product.price_xmr:.4f} XMR\n{product.description}\n\n"
                keyboard.append([InlineKeyboardButton(f"Buy {product.name} - {product.price_xmr:.4f} XMR", callback_data=f"buy_{product.id}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == update.effective_user.id).first()
            orders = session.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(10).all()
            if not orders:
                await update.message.reply_text("📋 You don't have any orders yet.")
                return
            text = "📋 **Your Recent Orders:**\n\n"
            for order in orders:
                product = session.query(Product).filter(Product.id == order.product_id).first()
                status_emoji = {
                    "pending": "⏳",
                    "paid": "✅",
                    "confirmed": "🎉",
                    "completed": "📦",
                    "expired": "❌",
                }
                text += f"{status_emoji.get(order.status, '📝')} **{product.name}**\n💰 {order.amount_xmr:.4f} XMR\n📅 {order.created_at.strftime('%Y-%m-%d %H:%M')}\n**Status:** {order.status.capitalize()}\n\n"
            keyboard = [[InlineKeyboardButton("🛍️ Browse Products", callback_data="show_products")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        if data == "show_products":
            await self._show_products_callback(query)
        elif data == "my_orders":
            await self._show_orders_callback(query)
        elif data.startswith("buy_"):
            product_id = int(data.split("_")[1])
            await self._handle_purchase(query, product_id)
        elif data.startswith("check_payment_"):
            order_id = int(data.split("_")[2])
            await self._check_payment(query, order_id)

    async def _show_products_callback(self, query):
        with Session() as session:
            products = session.query(Product).filter(Product.is_available == True).all()
            if not products:
                await query.edit_message_text("❌ No products available at the moment.")
                return
            text = "🛍️ **Available Products:**\n\n"
            keyboard = []
            for product in products:
                text += f"**{product.name}**\n💰 {product.price_xmr:.4f} XMR\n{product.description}\n\n"
                keyboard.append([InlineKeyboardButton(f"Buy {product.name} - {product.price_xmr:.4f} XMR", callback_data=f"buy_{product.id}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def _handle_purchase(self, query, product_id):
        with Session() as session:
            product = session.query(Product).filter(Product.id == product_id).first()
            user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
            if not product:
                await query.edit_message_text("❌ Product not found.")
                return
            address_info = self.monero.create_address(product_id)
            if not address_info:
                await query.edit_message_text("❌ Error generating payment address. Please try again.")
                return
            order = Order(
                user_id=user.id,
                product_id=product.id,
                amount_xmr=product.price_xmr,
                payment_address=address_info["integrated_address"],
                payment_id=address_info["payment_id"],
            )
            session.add(order)
            session.commit()
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(address_info["integrated_address"])
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, "PNG")
            bio.seek(0)
            payment_text = (
                f"💰 **Payment Details**\n\n"
                f"**Product:** {product.name}\n"
                f"**Amount:** {product.price_xmr:.4f} XMR\n"
                f"**Address:** `{address_info['integrated_address']}`\n\n"
                f"**Instructions:**\n"
                f"1️⃣ Send exactly **{product.price_xmr:.4f} XMR**\n"
                f"2️⃣ Click 'Check Payment' after sending\n"
                f"3️⃣ Product delivered automatically 🎉\n\n"
                f"⏰ Payment expires in 30 minutes"
            )
            keyboard = [
                [InlineKeyboardButton("🔍 Check Payment", callback_data=f"check_payment_{order.id}")],
                [InlineKeyboardButton("🛍️ Back to Products", callback_data="show_products")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_photo(photo=bio, caption=payment_text, reply_markup=reply_markup, parse_mode="Markdown")
            await query.edit_message_text("✅ Order created! Check payment details below.")

    async def _check_payment(self, query, order_id):
        with Session() as session:
            order = session.query(Order).filter(Order.id == order_id).first()
            product = session.query(Product).filter(Product.id == order.product_id).first()
            payment_info = self.monero.check_payment(order.payment_address, order.amount_xmr)
            if payment_info:
                if payment_info["confirmations"] >= config.CONFIRMATIONS_REQUIRED:
                    order.status = "confirmed"
                    payment = Payment(
                        order_id=order.id,
                        tx_hash=payment_info["tx_hash"],
                        amount_xmr=float(payment_info["amount"]),
                        confirmations=payment_info["confirmations"],
                    )
                    session.add(payment)
                    session.commit()
                    await query.edit_message_text(
                        f"✅ **Payment Confirmed!**\n\n**Product:** {product.name}\n**Transaction:** `{payment_info['tx_hash']}`\n**Confirmations:** {payment_info['confirmations']}\n\n🎉 Delivered!",
                        parse_mode="Markdown",
                    )
                else:
                    await query.edit_message_text(
                        f"⏳ Payment pending ({payment_info['confirmations']}/{config.CONFIRMATIONS_REQUIRED}) confirmations.",
                        parse_mode="Markdown",
                    )
            else:
                await query.answer("❌ No payment received yet.")

    async def _show_orders_callback(self, query):
        await self.show_orders(query, None)

    async def _register_user(self, telegram_user):
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == telegram_user.id).first()
            if not user:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    last_name=telegram_user.last_name,
                )
                session.add(user)
                session.commit()
            return user

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Please use commands or buttons. Use /start to begin.")


# Instantiate the bot
bot = MoneroBot()
loop = asyncio.get_event_loop()
loop.run_until_complete(bot.application.bot.set_webhook(WEBHOOK_URL))
logger.info(f"✅ Webhook set: {WEBHOOK_URL}")


@app.post(f"/webhook/{config.BOT_TOKEN}")
async def process_update(request: Request):
    """Endpoint Telegram calls with updates."""
    update = Update.de_json(await request.json(), bot.application.bot)
    await bot.application.process_update(update)
    return {"ok": True}


@app.get("/")
def home():
    return {"message": "🤖 Monero Telegram Bot Running via FastAPI"}


# Seed products once
def seed_products():
    with Session() as session:
        if session.query(Product).count() == 0:
            session.add_all([
                Product(name="XMR Hoodie", description="Stylish Monero hoodie.", price_xmr=0.005, image_url="", is_available=True),
                Product(name="XMR Cap", description="Classic Monero cap.", price_xmr=0.002, image_url="", is_available=True),
                Product(name="XMR Stickers", description="10-pack Monero stickers.", price_xmr=0.0005, image_url="", is_available=True)
            ])
            session.commit()
            print("✅ Seeded products.")


if __name__ == "__main__":
    seed_products()
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
