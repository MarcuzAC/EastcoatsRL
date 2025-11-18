import os
import io
import qrcode
import logging
import asyncio
import requests
import time
from datetime import datetime, timedelta
from typing import Dict, Any
from contextlib import asynccontextmanager
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

import config
from database import Session, User, Product, Order, Payment, ShippingAddress, Cart, CartItem, OrderItem
from monero_handler import MoneroHandler

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -------------------------
# Rate Limiter
# -------------------------
class RateLimiter:
    def __init__(self, max_calls: int = 10, period: int = 60):
        self.calls = defaultdict(list)
        self.max_calls = max_calls
        self.period = period

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        self.calls[user_id] = [t for t in self.calls[user_id] if now - t < self.period]
        if len(self.calls[user_id]) >= self.max_calls:
            return False
        self.calls[user_id].append(now)
        return True

rate_limiter = RateLimiter()

# -------------------------
# XMR Price Helper
# -------------------------
class XMRPrice:
    @staticmethod
    def get_xmr_price() -> float:
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=usd",
                timeout=10
            )
            data = response.json()
            return data.get("monero", {}).get("usd", 120.0)
        except Exception as e:
            logger.warning(f"Failed to fetch XMR price: {e}, using default $120")
            return 120.0

    @staticmethod
    def xmr_to_usd(xmr_amount: float) -> float:
        usd_price = XMRPrice.get_xmr_price()
        return xmr_amount * usd_price

# -------------------------
# MoneroBot class
# -------------------------
class MoneroBot:
    def __init__(self):
        self.application = None
        self.monero = MoneroHandler()
        self.user_states: Dict[int, Dict[str, Any]] = {}
        self._is_running = False
        self._checkout_lock: set[int] = set()

    async def initialize(self):
        if self.application is None:
            self.application = Application.builder().token(config.BOT_TOKEN).build()
            self.setup_handlers()

    def setup_handlers(self):
        self.application.add_error_handler(self.error_handler)

        # Command handlers (higher priority)
        self.application.add_handler(CommandHandler("start", self.start), group=0)
        self.application.add_handler(CommandHandler("products", self.show_products), group=0)
        self.application.add_handler(CommandHandler("cart", self.show_cart), group=0)
        self.application.add_handler(CommandHandler("orders", self.show_orders), group=0)
        self.application.add_handler(CommandHandler("clear_cart", self.clear_cart), group=0)
        self.application.add_handler(CommandHandler("cancel", self.cancel_operation), group=0)
        self.application.add_handler(CommandHandler("debug_state", self.debug_state), group=0)  # Debug command

        # Callback & message handlers
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start_webhook(self, webhook_url: str):
        await self.initialize()
        await self.application.initialize()
        await self.application.start()
        await self.application.bot.set_webhook(webhook_url)
        self._is_running = True
        logger.info(f"Webhook set to: {webhook_url}")

    async def start_polling(self):
        await self.initialize()
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        self._is_running = True
        logger.info("Bot started with polling")

    async def shutdown(self):
        if self.application and self._is_running:
            if self.application.updater and self.application.updater.running:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            self._is_running = False
            logger.info("Bot shutdown complete")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Update {update} caused error {context.error}", exc_info=True)

    def get_user_state(self, user_id: int) -> Dict[str, Any]:
        if user_id not in self.user_states:
            self.user_states[user_id] = {
                'created_at': time.time()
            }
        else:
            # Clear state if it's older than 1 hour (stuck prevention)
            state = self.user_states[user_id]
            if time.time() - state.get('created_at', 0) > 3600:
                logger.info(f"Clearing stale state for user {user_id}")
                self.user_states[user_id] = {'created_at': time.time()}
        
        return self.user_states[user_id]

    def clear_user_state(self, user_id: int):
        if user_id in self.user_states:
            del self.user_states[user_id]
            logger.info(f"Cleared state for user {user_id}")

    def format_price_with_usd(self, xmr_amount: float) -> str:
        usd_amount = XMRPrice.xmr_to_usd(xmr_amount)
        return f"{xmr_amount:.6f} XMR (≈${usd_amount:.2f} USD)"

    async def _safe_edit(self, query, text: str, reply_markup=None, **kwargs):
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, **kwargs)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await query.answer()
                return
            raise

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await self._register_user(user)
        self.clear_user_state(user.id)
        self._checkout_lock.discard(user.id)

        welcome_text = (
            "🌟 *Welcome to Crypto Pharmacy Bot!* 🌟\n\n"
            "• *Browse Products*: Use /products to see available medications\n"
            "• *Add to Cart*: Build your order with multiple items\n"
            "• *Pay with Monero*: Secure and private cryptocurrency payments\n"
            "• *Discreet Shipping*: Professional packaging and delivery\n\n"
            "*Commands:*\n"
            "📋 /products - Browse available products\n"
            "🛒 /cart - View your cart\n"
            "🗑️ /clear_cart - Empty your cart\n"
            "📦 /orders - View your orders\n"
            "❌ /cancel - Cancel current operation\n\n"
            "Start shopping now! 🛍️"
        )

        keyboard = [
            [InlineKeyboardButton("📋 Browse Products", callback_data="show_products")],
            [InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")],
            [InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.message:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await self._safe_edit(update.callback_query, welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

    async def debug_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Debug command to check user state"""
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        
        debug_info = f"User State Debug:\n"
        debug_info += f"User ID: {user_id}\n"
        debug_info += f"State: {user_state}\n"
        debug_info += f"In checkout lock: {user_id in self._checkout_lock}\n"
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if user and user.cart:
                debug_info += f"Cart items: {len(user.cart.cart_items)}\n"
                for item in user.cart.cart_items:
                    debug_info += f"  - {item.product.name} x {item.quantity}\n"
            else:
                debug_info += "No cart found\n"
        
        await update.message.reply_text(f"```{debug_info}```", parse_mode="Markdown")

    async def show_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.clear_user_state(update.effective_user.id)
        await self._show_products_common(update)

    async def _show_products_common(self, update: Update):
        with Session() as session:
            products = session.query(Product).filter(Product.is_available == True).all()
            if not products:
                msg = "No products available at the moment."
                if update.message:
                    await update.message.reply_text(msg)
                else:
                    await self._safe_edit(update.callback_query, msg)
                return

            text = "📋 *Available Products:*\n\n"
            keyboard = []
            for p in products:
                text += f"*{p.name}*\n"
                text += f"💎 {self.format_price_with_usd(p.price_xmr)}\n"
                if p.description:
                    text += f"📝 {p.description}\n"
                text += "\n"
                keyboard.append([
                    InlineKeyboardButton(f"➕ Add {p.name}", callback_data=f"add_to_cart_{p.id}"),
                    InlineKeyboardButton("🔍 Details", callback_data=f"product_details_{p.id}")
                ])
            keyboard.append([InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await self._safe_edit(update.callback_query, text, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.clear_user_state(user_id)

        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if not user or not user.cart or not user.cart.cart_items:
                msg = "🛒 Your cart is empty.\n\nUse /products to browse and add items."
                if update.message:
                    await update.message.reply_text(msg)
                else:
                    await self._safe_edit(update.callback_query, msg)
                return

            cart = user.cart
            total = sum(item.product.price_xmr * item.quantity for item in cart.cart_items)
            text = "🛒 *Your Shopping Cart*\n\n"
            for item in cart.cart_items:
                p = item.product
                subtotal = p.price_xmr * item.quantity
                text += f"*{p.name}*\n"
                text += f"💎 {self.format_price_with_usd(p.price_xmr)} × {item.quantity} = {self.format_price_with_usd(subtotal)}\n\n"
            text += f"*💰 Total: {self.format_price_with_usd(total)}*\n\n"

            keyboard = [
                [InlineKeyboardButton("📋 Add More Items", callback_data="show_products"),
                 InlineKeyboardButton("🗑️ Clear Cart", callback_data="clear_cart")],
                [InlineKeyboardButton("🚀 Proceed to Checkout", callback_data="start_checkout")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await self._safe_edit(update.callback_query, text, reply_markup=reply_markup, parse_mode="Markdown")

    async def clear_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if user and user.cart:
                session.delete(user.cart)
                session.commit()
            msg = "🗑️ Your cart has been cleared."
            if update.message:
                await update.message.reply_text(msg)
            else:
                await self._safe_edit(update.callback_query, msg)

    async def show_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.clear_user_state(update.effective_user.id)
        await self._show_orders_common(update)

    async def _show_orders_common(self, update: Update):
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == update.effective_user.id).first()
            if not user:
                msg = "📦 You don't have any orders yet."
                if update.message:
                    await update.message.reply_text(msg)
                else:
                    await self._safe_edit(update.callback_query, msg)
                return

            orders = session.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(10).all()
            if not orders:
                msg = "📦 You don't have any orders yet."
                if update.message:
                    await update.message.reply_text(msg)
                else:
                    await self._safe_edit(update.callback_query, msg)
                return

            text = "📦 *Your Recent Orders:*\n\n"
            for o in orders:
                status_emoji = {"pending": "⏳", "paid": "💰", "confirmed": "✅", "shipped": "🚚", "completed": "🎉", "expired": "❌"}
                text += f"{status_emoji.get(o.status, '❓')} *Order #{o.id}*\n"
                text += f"💎 {self.format_price_with_usd(o.total_amount_xmr)}\n"
                text += f"📅 {o.created_at.strftime('%Y-%m-%d %H:%M')}\n"
                text += f"*Status:* {o.status.capitalize()}\n"
                text += f"*Items:* {len(o.order_items)}\n"
                if o.shipping_address:
                    text += f"*Shipping:* {o.shipping_address.city}, {o.shipping_address.state}\n"
                text += "\n"

            keyboard = [
                [InlineKeyboardButton("📋 Browse Products", callback_data="show_products")],
                [InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await self._safe_edit(update.callback_query, text, reply_markup=reply_markup, parse_mode="Markdown")

    async def cancel_operation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.clear_user_state(user_id)
        self._checkout_lock.discard(user_id)
        msg = "❌ Operation cancelled. Use /start to begin again."
        if update.message:
            await update.message.reply_text(msg)
        else:
            await self._safe_edit(update.callback_query, msg)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if not rate_limiter.is_allowed(query.from_user.id):
            await query.answer("Too many requests. Try again later.")
            return

        if data == "show_products":
            await self._show_products_common(update)
        elif data == "view_cart":
            await self.show_cart(update, context)
        elif data == "my_orders":
            await self._show_orders_common(update)
        elif data == "clear_cart":
            await self.clear_cart(update, context)
        elif data == "start_checkout":
            await self._start_checkout(update, context)
        elif data.startswith("add_to_cart_"):
            product_id = int(data[len("add_to_cart_"):])
            await self._add_to_cart(update, context, product_id)
        elif data.startswith("product_details_"):
            product_id = int(data[len("product_details_"):])
            await self._show_product_details(update, context, product_id)
        elif data.startswith("check_payment_"):
            order_id = int(data[len("check_payment_"):])
            await self._check_payment(update, context, order_id)
        elif data.startswith("order_details_"):
            order_id = int(data[len("order_details_"):])
            await self._show_order_details(update, context, order_id)

    async def _add_to_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            product = session.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.answer("Product not found")
                return
            if not user.cart:
                user.cart = Cart()
                session.add(user.cart)
                session.flush()
            existing = session.query(CartItem).filter(CartItem.cart_id == user.cart.id, CartItem.product_id == product_id).first()
            if existing:
                existing.quantity += 1
            else:
                session.add(CartItem(cart_id=user.cart.id, product_id=product_id, quantity=1))
            session.commit()
            await query.answer(f"✅ {product.name} added to cart!")

    async def _show_product_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
        query = update.callback_query
        with Session() as session:
            product = session.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.answer("Product not found")
                return
            text = f"*{product.name}*\n\n"
            text += f"*💰 Price:* {self.format_price_with_usd(product.price_xmr)}\n\n"
            if product.description:
                text += f"*📝 Description:* {product.description}\n\n"
            text += "Available for one-time purchase."
            keyboard = [
                [InlineKeyboardButton("➕ Add to Cart", callback_data=f"add_to_cart_{product.id}")],
                [InlineKeyboardButton("⬅️ Back to Products", callback_data="show_products")],
            ]
            await self._safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def _start_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id

        if user_id in self._checkout_lock:
            await query.answer("Checkout already in progress")
            return
        self._checkout_lock.add(user_id)

        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if not user or not user.cart or not user.cart.cart_items:
                await query.answer("Your cart is empty")
                self._checkout_lock.discard(user_id)
                return

            cart = user.cart
            total_amount = sum(item.product.price_xmr * item.quantity for item in cart.cart_items)

            user_state = self.get_user_state(user_id)
            user_state.update({
                'checkout_flow': True,
                'current_step': 'full_name'
            })

            await self._safe_edit(
                query,
                f"🚀 *Proceeding to Checkout*\n\n"
                f"*💰 Cart Total:* {self.format_price_with_usd(total_amount)}\n"
                f"*📦 Items:* {len(cart.cart_items)}\n\n"
                "Please provide your shipping information:\n\n"
                "*Step 1 of 6: Full Name*\n"
                "Please enter your full name:",
                parse_mode="Markdown"
            )

    def _validate_input(self, step: str, value: str) -> tuple[bool, str]:
        value = value.strip()
        if step == 'full_name' and len(value) < 3:
            return False, "Full name must be at least 3 characters"
        if step == 'street_address' and len(value) < 5:
            return False, "Street address too short"
        if step == 'city' and len(value) < 2:
            return False, "City too short"
        if step == 'state' and len(value) < 2:
            return False, "State too short"
        if step == 'zip_code':
            # More flexible ZIP code validation
            if not value or len(value) < 3:
                return False, "ZIP code must be at least 3 characters"
            # Allow alphanumeric for international ZIP codes
            if not value.replace(' ', '').replace('-', '').isalnum():
                return False, "ZIP code can only contain letters, numbers, and hyphens"
        return True, ""

    async def _collect_shipping_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        
        logger.info(f"Processing message for user {user_id}, state: {user_state}")
        
        if not user_state.get('checkout_flow'):
            logger.info(f"No checkout flow for user {user_id}")
            return

        current_step = user_state.get('current_step')
        text = update.message.text.strip()

        logger.info(f"User {user_id} at step '{current_step}' entered: {text}")

        steps = {
            'full_name': {'field': 'full_name', 'next_step': 'street_address', 'prompt': "*Step 2 of 6: Street Address*\nPlease enter your street address:"},
            'street_address': {'field': 'street_address', 'next_step': 'apt_number', 'prompt': "*Step 3 of 6: Apartment/Unit Number*\nPlease enter your apartment or unit number (or type 'none' if not applicable):"},
            'apt_number': {'field': 'apt_number', 'next_step': 'city', 'prompt': "*Step 4 of 6: City*\nPlease enter your city:"},
            'city': {'field': 'city', 'next_step': 'state', 'prompt': "*Step 5 of 6: State*\nPlease enter your state:"},
            'state': {'field': 'state', 'next_step': 'zip_code', 'prompt': "*Step 6 of 6: ZIP Code*\nPlease enter your ZIP code:"},
            'zip_code': {'field': 'zip_code', 'next_step': 'complete', 'prompt': None}
        }

        if current_step not in steps:
            logger.warning(f"Unknown step '{current_step}' for user {user_id}")
            await update.message.reply_text("Something went wrong. Please use /cancel and try again.")
            self.clear_user_state(user_id)
            self._checkout_lock.discard(user_id)
            return

        valid, msg = self._validate_input(current_step, text)
        if not valid:
            logger.info(f"Validation failed for user {user_id} at step {current_step}: {msg}")
            await update.message.reply_text(f"❌ {msg}\n\nPlease try again:", parse_mode="Markdown")
            return

        # Store the validated input
        user_state[steps[current_step]['field']] = text
        logger.info(f"User {user_id} completed step {current_step}")

        if steps[current_step]['next_step'] == 'complete':
            logger.info(f"User {user_id} completed all shipping steps, creating order...")
            await self._create_order_from_cart(update, context)
        else:
            user_state['current_step'] = steps[current_step]['next_step']
            next_prompt = steps[steps[current_step]['next_step']]['prompt']
            if next_prompt:
                logger.info(f"Moving user {user_id} to step {user_state['current_step']}")
                await update.message.reply_text(next_prompt, parse_mode="Markdown")

    async def _create_order_from_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)

        try:
            logger.info(f"Creating order for user {user_id}")
            
            with Session() as session:
                user = session.query(User).filter(User.telegram_id == user_id).first()
                if not user or not user.cart or not user.cart.cart_items:
                    error_msg = "Error: Your cart is empty or user not found."
                    logger.error(error_msg)
                    await update.message.reply_text(error_msg)
                    self.clear_user_state(user_id)
                    self._checkout_lock.discard(user_id)
                    return

                cart = user.cart
                total_amount = sum(item.product.price_xmr * item.quantity for item in cart.cart_items)
                item_count = len(cart.cart_items)

                shipping_address = ShippingAddress(
                    full_name=user_state['full_name'],
                    street_address=user_state['street_address'],
                    apt_number=user_state['apt_number'] if user_state['apt_number'].lower() != 'none' else None,
                    city=user_state['city'],
                    state=user_state['state'],
                    zip_code=user_state['zip_code']
                )
                session.add(shipping_address)
                session.flush()

                payment_data = self.monero.create_payment_request(f"Order #{user.id}", total_amount)
                if not payment_data:
                    await update.message.reply_text("Error generating payment. Please try again.")
                    self.clear_user_state(user_id)
                    self._checkout_lock.discard(user_id)
                    return

                order = Order(
                    user_id=user.id,
                    total_amount_xmr=total_amount,
                    payment_address=payment_data.get("integrated_address"),
                    payment_id=payment_data.get("payment_id"),
                    payment_request=payment_data.get("payment_request"),
                    shipping_address_id=shipping_address.id,
                    expires_at=datetime.utcnow() + timedelta(minutes=30)
                )
                session.add(order)
                session.flush()

                for cart_item in cart.cart_items:
                    session.add(OrderItem(
                        order_id=order.id,
                        product_id=cart_item.product_id,
                        quantity=cart_item.quantity,
                        price_xmr=cart_item.product.price_xmr
                    ))

                session.delete(cart)
                session.commit()

                qr = qrcode.QRCode(version=1, box_size=8, border=4)
                qr.add_data(payment_data.get("payment_request"))
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                bio = io.BytesIO()
                img.save(bio, "PNG")
                bio.seek(0)

                shipping_summary = (
                    f"*📦 Shipping to:*\n"
                    f"{shipping_address.full_name}\n"
                    f"{shipping_address.street_address}\n"
                )
                if shipping_address.apt_number:
                    shipping_summary += f"Apt/Unit: {shipping_address.apt_number}\n"
                shipping_summary += f"{shipping_address.city}, {shipping_address.state} {shipping_address.zip_code}"

                order_summary = ""
                for item in order.order_items:
                    order_summary += f"• {item.product.name} × {item.quantity} = {self.format_price_with_usd(item.price_xmr * item.quantity)}\n"

                payment_text = (
                    f"*💰 Payment Request*\n\n"
                    f"{order_summary}\n"
                    f"*💰 Total Amount:* {self.format_price_with_usd(total_amount)}\n\n"
                    f"{shipping_summary}\n\n"
                    "*📋 Instructions:*\n"
                    "1. Scan the QR code or copy the payment request\n"
                    "2. Use a Monero wallet that supports payment requests\n"
                    "3. Click \"Check Payment\" after sending\n"
                    "4. Your order will be shipped after confirmation\n\n"
                    "⏰ Payment expires in 30 minutes"
                )

                keyboard = [
                    [InlineKeyboardButton("🔍 Check Payment", callback_data=f"check_payment_{order.id}")],
                    [InlineKeyboardButton("📦 Order Details", callback_data=f"order_details_{order.id}")],
                    [InlineKeyboardButton("🛍️ Continue Shopping", callback_data="show_products")],
                ]
                await update.message.reply_photo(
                    photo=bio,
                    caption=payment_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )

                logger.info(f"Order #{order.id} created successfully for user {user_id}")
                self.clear_user_state(user_id)
                self._checkout_lock.discard(user_id)

        except Exception as e:
            logger.error(f"Error creating order for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ An error occurred while creating your order. Please try again or contact support."
            )
            self.clear_user_state(user_id)
            self._checkout_lock.discard(user_id)

    async def _check_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
        query = update.callback_query
        with Session() as session:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                await query.answer("Order not found")
                return

            if datetime.utcnow() > order.expires_at:
                order.status = "expired"
                session.commit()
                await self._safe_edit(query, "❌ Payment expired. Please create a new order.")
                return

            payment_info = self.monero.check_payment(order.payment_id, order.total_amount_xmr)
            if payment_info and payment_info.get("confirmations", 0) >= getattr(config, "CONFIRMATIONS_REQUIRED", 10):
                order.status = "confirmed"
                order.confirmed_at = datetime.utcnow()
                session.add(Payment(
                    order_id=order.id,
                    tx_hash=payment_info.get("tx_hash"),
                    amount_xmr=float(payment_info.get("amount", 0.0)),
                    confirmations=payment_info.get("confirmations", 0),
                ))
                session.commit()

                items_summary = "*📦 Order Items:*\n"
                for item in order.order_items:
                    items_summary += f"• {item.product.name} × {item.quantity}\n"

                shipping_info = ""
                if order.shipping_address:
                    a = order.shipping_address
                    shipping_info = f"\n*📦 Shipping Address:*\n{a.full_name}\n{a.street_address}\n"
                    if a.apt_number:
                        shipping_info += f"{a.apt_number}\n"
                    shipping_info += f"{a.city}, {a.state} {a.zip_code}"

                await self._safe_edit(
                    query,
                    f"✅ *Payment Confirmed!*\n\n"
                    f"*📦 Order #* {order.id}\n"
                    f"*🔗 Transaction:* `{payment_info.get('tx_hash')}`\n"
                    f"*✅ Confirmations:* {payment_info.get('confirmations')}\n"
                    f"*💰 Amount:* {self.format_price_with_usd(payment_info.get('amount', 0.0))}\n"
                    f"{items_summary}\n{shipping_info}\n\n"
                    "Your order will be shipped soon!",
                    parse_mode="Markdown"
                )
            elif payment_info:
                await self._safe_edit(
                    query,
                    f"⏳ *Payment Received - Pending*\n\n"
                    f"*📦 Order #* {order.id}\n"
                    f"*💰 Amount:* {self.format_price_with_usd(payment_info.get('amount', 0.0))}\n"
                    f"*🔗 Tx:* `{payment_info.get('tx_hash')}`\n"
                    f"*✅ Confirmations:* {payment_info.get('confirmations', 0)}/{getattr(config, 'CONFIRMATIONS_REQUIRED', 10)}\n\n"
                    "Waiting for confirmations...",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Again", callback_data=f"check_payment_{order.id}")]]),
                    parse_mode="Markdown"
                )
            else:
                await query.answer("No payment received yet.")

    async def _show_order_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
        query = update.callback_query
        with Session() as session:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                await query.answer("Order not found")
                return
            text = f"*📦 Order #{order.id}*\n\n"
            text += f"*📊 Status:* {order.status.capitalize()}\n"
            text += f"*💰 Total:* {self.format_price_with_usd(order.total_amount_xmr)}\n"
            text += f"*📅 Created:* {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            text += f"*⏰ Expires:* {order.expires_at.strftime('%Y-%m-%d %H:%M')}\n\n"
            text += "*📦 Items:*\n"
            for item in order.order_items:
                text += f"• {item.product.name} × {item.quantity} = {self.format_price_with_usd(item.price_xmr * item.quantity)}\n"
            text += f"\n*📦 Shipping Address:*\n"
            if order.shipping_address:
                a = order.shipping_address
                text += f"{a.full_name}\n{a.street_address}\n"
                if a.apt_number:
                    text += f"{a.apt_number}\n"
                text += f"{a.city}, {a.state} {a.zip_code}\n"
            keyboard = [
                [InlineKeyboardButton("🔍 Check Payment", callback_data=f"check_payment_{order.id}")],
                [InlineKeyboardButton("📦 All Orders", callback_data="my_orders")],
            ]
            await self._safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    async def _register_user(self, telegram_user):
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == telegram_user.id).first()
            if not user:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    last_name=telegram_user.last_name,
                    created_at=datetime.utcnow(),
                )
                session.add(user)
                session.commit()
            return user

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        if user_state.get('checkout_flow'):
            await self._collect_shipping_info(update, context)
        else:
            await update.message.reply_text(
                "Please use the menu buttons or commands.\n\n"
                "📋 /start  🛒 /products  📦 /cart  📦 /orders  🗑️ /clear_cart  ❌ /cancel"
            )

# -------------------------
# Background Job: Expire Orders
# -------------------------
async def expire_old_orders(context: ContextTypes.DEFAULT_TYPE):
    """Fixed function with context parameter"""
    try:
        with Session() as session:
            expired = session.query(Order).filter(
                Order.status == "pending",
                Order.expires_at < datetime.utcnow()
            ).all()
            for o in expired:
                o.status = "expired"
            if expired:
                session.commit()
                logger.info(f"Expired {len(expired)} orders.")
    except Exception as e:
        logger.error(f"Error in expire_old_orders: {e}")

# -------------------------
# Bot Instance
# -------------------------
bot = MoneroBot()

def seed_products():
    with Session() as session:
        if session.query(Product).count() == 0:
            products = [
                Product(name="100ug Fluloprazolam Sheets", description="High quality research chemical", price_xmr=0.0035, is_available=True),
                Product(name="250ug Fluloprazolam Sheets", description="Premium research chemical", price_xmr=0.0070, is_available=True),
                Product(name="Dermorphin 5mg Vials", description="Pharmaceutical grade", price_xmr=0.0035, is_available=True),
                Product(name="100ct Adderall", description="Pharmaceutical grade", price_xmr=0.0070, is_available=True),
                Product(name="Tadalafil Powder 1g", description="High purity", price_xmr=0.0035, is_available=True),
                Product(name="100mg Fluloprazolam Powder", description="Research chemical", price_xmr=0.0140, is_available=True),
                Product(name="Bromnordiazepam 1g", description="Research chemical", price_xmr=0.0140, is_available=True),
                Product(name="Promethazine Clearance 33.8g", description="Clearance sale", price_xmr=0.0220, is_available=True),
            ]
            session.add_all(products)
            session.commit()
            logger.info("Seeded products.")
        else:
            logger.info("Products already exist.")

# -------------------------
# FastAPI Lifespan
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_products()
    port = int(os.getenv("PORT", 8000))
    webhook_url = os.getenv("WEBHOOK_URL")

    if webhook_url:
        logger.info("Starting in PRODUCTION mode with webhook")
        await bot.start_webhook(webhook_url)
    else:
        logger.info("Starting in DEVELOPMENT mode with polling")
        await bot.start_polling()

    # Start expiry job - FIXED: pass the function reference, not call it
    if bot.application and bot.application.job_queue:
        bot.application.job_queue.run_repeating(
            expire_old_orders, 
            interval=300, 
            first=10
        )
        logger.info("Expiry job scheduled successfully")

    logger.info("Bot initialized and ready!")
    yield
    logger.info("Shutting down bot...")
    await bot.shutdown()

# -------------------------
# FastAPI App
# -------------------------
app = FastAPI(title="Pharmacy Telegram Bot", lifespan=lifespan)

@app.get("/")
async def healthcheck():
    return {"status": "ok", "message": "Bot is running."}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"WEBHOOK RECEIVED - Update ID: {data.get('update_id')}")
        update = Update.de_json(data, bot.application.bot)
        await bot.application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)