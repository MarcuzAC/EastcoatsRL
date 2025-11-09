import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import qrcode
import io
import json
from decimal import Decimal
import asyncio

import config
from database import Session, User, Product, Order, Payment
from monero_handler import MoneroHandler

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MoneroBot:
    def __init__(self):
        self.application = Application.builder().token(config.BOT_TOKEN).build()
        self.monero = MoneroHandler()
        self.setup_handlers()
        
    def setup_handlers(self):
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("products", self.show_products))
        self.application.add_handler(CommandHandler("orders", self.show_orders))
        
        # Callback query handlers
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
        # Message handlers
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await self._register_user(user)
        
        welcome_text = """
🤖 **Welcome to Crypto Store Bot!**

🛍️ **Browse Products**: Use /products to see available items  
💳 **Pay with Monero**: Secure and private cryptocurrency payments  
📦 **Instant Delivery**: Digital products delivered automatically  

**Commands:**  
/products - Browse available products  
/orders - View your orders  

Start shopping now! 🎉
        """
        
        keyboard = [
            [InlineKeyboardButton("🛍️ Browse Products", callback_data="show_products")],
            [InlineKeyboardButton("📋 My Orders", callback_data="my_orders")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

    async def show_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        with Session() as session:
            products = session.query(Product).filter(Product.is_available == True).all()
            
            if not products:
                await update.message.reply_text("❌ No products available at the moment.")
                return
            
            text = "🛍️ **Available Products:**\n\n"
            keyboard = []
            
            for product in products:
                text += f"**{product.name}**\n"
                text += f"💰 {product.price_xmr:.4f} XMR\n"
                text += f"{product.description}\n\n"
                
                keyboard.append([
                    InlineKeyboardButton(
                        f"Buy {product.name} - {product.price_xmr:.4f} XMR", 
                        callback_data=f"buy_{product.id}"
                    )
                ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def show_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles the /orders command"""
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
                    'pending': '⏳',
                    'paid': '✅',
                    'confirmed': '🎉',
                    'completed': '📦',
                    'expired': '❌'
                }
                text += f"{status_emoji.get(order.status, '📝')} **{product.name}**\n"
                text += f"💰 {order.amount_xmr:.4f} XMR\n"
                text += f"📅 {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
                text += f"**Status:** {order.status.capitalize()}\n\n"
            
            keyboard = [[InlineKeyboardButton("🛍️ Browse Products", callback_data="show_products")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

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
                text += f"**{product.name}**\n"
                text += f"💰 {product.price_xmr:.4f} XMR\n"
                text += f"{product.description}\n\n"
                
                keyboard.append([
                    InlineKeyboardButton(
                        f"Buy {product.name} - {product.price_xmr:.4f} XMR", 
                        callback_data=f"buy_{product.id}"
                    )
                ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

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
                payment_address=address_info['integrated_address'],
                payment_id=address_info['payment_id']
            )
            session.add(order)
            session.commit()
            
            # Generate QR code
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(address_info['integrated_address'])
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, 'PNG')
            bio.seek(0)
            
            payment_text = f"""
💰 **Payment Details**

**Product:** {product.name}  
**Amount:** {product.price_xmr:.4f} XMR  
**Address:** `{address_info['integrated_address']}`  

**Instructions:**  
1️⃣ Send exactly **{product.price_xmr:.4f} XMR** to the address above  
2️⃣ Click "Check Payment" after sending  
3️⃣ Your product will be delivered automatically after confirmation  

⏰ **Payment expires in 30 minutes**
            """
            
            keyboard = [
                [InlineKeyboardButton("🔍 Check Payment", callback_data=f"check_payment_{order.id}")],
                [InlineKeyboardButton("🛍️ Back to Products", callback_data="show_products")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.reply_photo(
                photo=bio,
                caption=payment_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            await query.edit_message_text("✅ Order created! Check the payment details below.")

    async def _check_payment(self, query, order_id):
        with Session() as session:
            order = session.query(Order).filter(Order.id == order_id).first()
            product = session.query(Product).filter(Product.id == order.product_id).first()
            
            if not order:
                await query.answer("Order not found")
                return
            
            payment_info = self.monero.check_payment(order.payment_address, order.amount_xmr)
            
            if payment_info:
                if payment_info['confirmations'] >= config.CONFIRMATIONS_REQUIRED:
                    order.status = 'confirmed'
                    payment = Payment(
                        order_id=order.id,
                        tx_hash=payment_info['tx_hash'],
                        amount_xmr=float(payment_info['amount']),
                        confirmations=payment_info['confirmations']
                    )
                    session.add(payment)
                    session.commit()
                    
                    delivery_text = f"""
✅ **Payment Confirmed!**

**Product:** {product.name}  
**Transaction:** `{payment_info['tx_hash']}`  
**Confirmations:** {payment_info['confirmations']}  

Your product has been delivered! 🎉
                    """
                    
                    await query.edit_message_text(delivery_text, parse_mode='Markdown')
                else:
                    pending_text = f"""
⏳ **Payment Received - Pending Confirmation**

**Product:** {product.name}  
**Amount:** {payment_info['amount']:.4f} XMR  
**Transaction:** `{payment_info['tx_hash']}`  
**Confirmations:** {payment_info['confirmations']}/{config.CONFIRMATIONS_REQUIRED}

Waiting for more confirmations...
                    """
                    
                    keyboard = [
                        [InlineKeyboardButton("🔍 Check Again", callback_data=f"check_payment_{order.id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(pending_text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await query.answer("❌ No payment received yet. Please send the exact amount to the address provided.")

    async def _show_orders_callback(self, query):
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
            orders = session.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(10).all()
            
            if not orders:
                await query.edit_message_text("📋 You don't have any orders yet.")
                return
            
            text = "📋 **Your Recent Orders:**\n\n"
            for order in orders:
                product = session.query(Product).filter(Product.id == order.product_id).first()
                status_emoji = {
                    'pending': '⏳',
                    'paid': '✅',
                    'confirmed': '🎉',
                    'completed': '📦',
                    'expired': '❌'
                }
                text += f"{status_emoji.get(order.status, '📝')} **{product.name}**\n"
                text += f"💰 {order.amount_xmr:.4f} XMR\n"
                text += f"📅 {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
                text += f"**Status:** {order.status.capitalize()}\n\n"
            
            keyboard = [[InlineKeyboardButton("🛍️ Browse Products", callback_data="show_products")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def _register_user(self, telegram_user):
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == telegram_user.id).first()
            if not user:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    last_name=telegram_user.last_name
                )
                session.add(user)
                session.commit()
            return user

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Please use the menu buttons or commands to interact with the bot. Use /start to see available options."
        )

    def run(self):
        logger.info("Bot is running...")
        self.application.run_polling()


# ✅ Add this helper function at the end
def seed_products():
    """Insert sample products if database is empty."""
    from database import Session, Product
    with Session() as session:
        if session.query(Product).count() == 0:
            sample_products = [
                Product(
                    name="XMR Hoodie",
                    description="Stylish Monero hoodie — perfect for privacy lovers.",
                    price_xmr=0.005,
                    image_url="https://i.imgur.com/hoodie.jpg",
                    is_available=True
                ),
                Product(
                    name="XMR Cap",
                    description="Black cap with the Monero logo — comfy and sleek.",
                    price_xmr=0.002,
                    image_url="https://i.imgur.com/cap.jpg",
                    is_available=True
                ),
                Product(
                    name="XMR Sticker Pack",
                    description="Set of 10 high-quality Monero stickers.",
                    price_xmr=0.0005,
                    image_url="https://i.imgur.com/stickers.jpg",
                    is_available=True
                )
            ]
            session.add_all(sample_products)
            session.commit()
            print("✅ Seeded sample products.")
        else:
            print("✅ Products already exist.")



if __name__ == '__main__':
    seed_products()
    bot = MoneroBot()
    bot.run()
