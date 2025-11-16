import os
import io
import qrcode
import logging
import asyncio
import requests
from datetime import datetime, timedelta
from typing import Dict, Any
from contextlib import asynccontextmanager

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

import config
from database import Session, User, Product, Order, Payment, ShippingAddress, Cart, CartItem, OrderItem
from monero_handler import MoneroHandler

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------
# XMR Price Helper
# -------------------------
class XMRPrice:
    @staticmethod
    def get_xmr_price() -> float:
        """Get current XMR to USD price from CoinGecko"""
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=usd",
                timeout=10
            )
            data = response.json()
            return data.get("monero", {}).get("usd", 120.0)  # Default fallback
        except Exception as e:
            logger.warning(f"Failed to fetch XMR price: {e}, using default $120")
            return 120.0  # Fallback price

    @staticmethod
    def xmr_to_usd(xmr_amount: float) -> float:
        """Convert XMR to USD"""
        usd_price = XMRPrice.get_xmr_price()
        return xmr_amount * usd_price

# -------------------------
# MoneroBot class
# -------------------------
class MoneroBot:
    def __init__(self):
        self.application = Application.builder().token(config.BOT_TOKEN).build()
        self.monero = MoneroHandler()
        self.user_states: Dict[int, Dict[str, Any]] = {}
        self.setup_handlers()

    def setup_handlers(self):
        # Add error handler first
        self.application.add_error_handler(self.error_handler)
        
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("products", self.show_products))
        self.application.add_handler(CommandHandler("cart", self.show_cart))
        self.application.add_handler(CommandHandler("orders", self.show_orders))
        self.application.add_handler(CommandHandler("clear_cart", self.clear_cart))
        self.application.add_handler(CommandHandler("cancel", self.cancel_operation))
        
        # Callback query handler
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
        # Message handler
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors gracefully"""
        logger.error(f"Update {update} caused error {context.error}")

    def get_user_state(self, user_id: int) -> Dict[str, Any]:
        if user_id not in self.user_states:
            self.user_states[user_id] = {}
        return self.user_states[user_id]

    def clear_user_state(self, user_id: int):
        if user_id in self.user_states:
            del self.user_states[user_id]

    def format_price_with_usd(self, xmr_amount: float) -> str:
        """Format XMR price with USD equivalent"""
        usd_amount = XMRPrice.xmr_to_usd(xmr_amount)
        return f"{xmr_amount:.6f} XMR (≈${usd_amount:.2f} USD)"

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        await self._register_user(user)
        self.clear_user_state(user.id)

        welcome_text = (
            "🛒 **Welcome to Crypto Pharmacy Bot!**\n\n"
            "💊 **Browse Products**: Use /products to see available medications\n"
            "🛒 **Add to Cart**: Build your order with multiple items\n"
            "💳 **Pay with Monero**: Secure and private cryptocurrency payments\n"
            "📦 **Discreet Shipping**: Professional packaging and delivery\n\n"
            "**Commands:**\n"
            "/products - Browse available products\n"
            "/cart - View your cart\n"
            "/clear_cart - Empty your cart\n"
            "/orders - View your orders\n"
            "/cancel - Cancel current operation\n\n"
            "Start shopping now! 🎉"
        )

        keyboard = [
            [InlineKeyboardButton("💊 Browse Products", callback_data="show_products")],
            [InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")],
            [InlineKeyboardButton("📋 My Orders", callback_data="my_orders")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.message:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.clear_user_state(update.effective_user.id)
        
        with Session() as session:
            products = session.query(Product).filter(Product.is_available == True).all()

            if not products:
                message_text = "❌ No products available at the moment."
                if update.message:
                    await update.message.reply_text(message_text)
                else:
                    await update.callback_query.edit_message_text(message_text)
                return

            text = "💊 **Available Products:**\n\n"
            keyboard = []

            for product in products:
                text += f"**{product.name}**\n"
                text += f"💰 {self.format_price_with_usd(product.price_xmr)}\n"
                if product.description:
                    text += f"📝 {product.description}\n"
                text += "\n"

                keyboard.append([
                    InlineKeyboardButton(f"🛒 Add {product.name}", callback_data=f"add_to_cart_{product.id}"),
                    InlineKeyboardButton(f"ℹ️ Details", callback_data=f"product_details_{product.id}")
                ])

            keyboard.append([InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def show_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.clear_user_state(user_id)
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if not user or not user.cart or not user.cart.cart_items:
                message_text = (
                    "🛒 Your cart is empty.\n\n"
                    "Use /products to browse and add items to your cart."
                )
                if update.message:
                    await update.message.reply_text(message_text)
                else:
                    await update.callback_query.edit_message_text(message_text)
                return

            cart = user.cart
            total_amount = 0.0
            text = "🛒 **Your Shopping Cart**\n\n"

            for item in cart.cart_items:
                product = item.product
                item_total = product.price_xmr * item.quantity
                total_amount += item_total
                
                text += f"**{product.name}**\n"
                text += f"💰 {self.format_price_with_usd(product.price_xmr)} × {item.quantity} = {self.format_price_with_usd(item_total)}\n\n"

            text += f"**Total: {self.format_price_with_usd(total_amount)}**\n\n"

            keyboard = [
                [
                    InlineKeyboardButton("➕ Add More Items", callback_data="show_products"),
                    InlineKeyboardButton("🗑️ Clear Cart", callback_data="clear_cart")
                ],
                [InlineKeyboardButton("🚀 Proceed to Checkout", callback_data="start_checkout")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def clear_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if user and user.cart:
                session.delete(user.cart)
                session.commit()
            
            message_text = "🗑️ Your cart has been cleared."
            
            if update.message:
                await update.message.reply_text(message_text)
            else:
                await update.callback_query.edit_message_text(message_text)

    async def show_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.clear_user_state(update.effective_user.id)
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == update.effective_user.id).first()
            if not user:
                message_text = "📋 You don't have any orders yet."
                if update.message:
                    await update.message.reply_text(message_text)
                else:
                    await update.callback_query.edit_message_text(message_text)
                return

            orders = session.query(Order).filter(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(10).all()

            if not orders:
                message_text = "📋 You don't have any orders yet."
                if update.message:
                    await update.message.reply_text(message_text)
                else:
                    await update.callback_query.edit_message_text(message_text)
                return

            text = "📋 **Your Recent Orders:**\n\n"
            for order in orders:
                status_emoji = {
                    "pending": "⏳",
                    "paid": "✅",
                    "confirmed": "🎉",
                    "shipped": "🚚",
                    "completed": "📦",
                    "expired": "❌",
                }
                
                text += f"{status_emoji.get(order.status, '📝')} **Order #{order.id}**\n"
                text += f"💰 {self.format_price_with_usd(order.total_amount_xmr)}\n"
                text += f"📅 {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
                text += f"**Status:** {order.status.capitalize()}\n"
                
                # Show item count
                item_count = len(order.order_items)
                text += f"**Items:** {item_count} product{'s' if item_count != 1 else ''}\n"
                
                if order.shipping_address:
                    text += f"**Shipping:** {order.shipping_address.city}, {order.shipping_address.state}\n"
                text += "\n"

            keyboard = [
                [InlineKeyboardButton("💊 Browse Products", callback_data="show_products")],
                [InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.message:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def cancel_operation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.clear_user_state(user_id)
        message_text = "❌ Operation cancelled. Use /start to begin again."
        
        if update.message:
            await update.message.reply_text(message_text)
        else:
            await update.callback_query.edit_message_text(message_text)

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "show_products":
            await self._show_products_callback(update, context)
        elif data == "view_cart":
            await self.show_cart(update, context)
        elif data == "my_orders":
            await self._show_orders_callback(update, context)
        elif data == "clear_cart":
            await self.clear_cart(update, context)
        elif data == "start_checkout":
            await self._start_checkout(update, context)
        elif data.startswith("add_to_cart_"):
            product_id = int(data.split("_")[3])
            await self._add_to_cart(update, context, product_id)
        elif data.startswith("product_details_"):
            product_id = int(data.split("_")[2])
            await self._show_product_details(update, context, product_id)
        elif data.startswith("check_payment_"):
            order_id = int(data.split("_")[2])
            await self._check_payment(update, context, order_id)
        elif data.startswith("order_details_"):
            order_id = int(data.split("_")[2])
            await self._show_order_details(update, context, order_id)

    async def _add_to_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
        query = update.callback_query
        user_id = query.from_user.id
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            product = session.query(Product).filter(Product.id == product_id).first()

            if not product:
                await query.answer("❌ Product not found")
                return

            # Get or create cart
            if not user.cart:
                user.cart = Cart()
                session.add(user.cart)
                session.flush()

            # Check if product already in cart
            existing_item = session.query(CartItem).filter(
                CartItem.cart_id == user.cart.id,
                CartItem.product_id == product_id
            ).first()

            if existing_item:
                existing_item.quantity += 1
            else:
                cart_item = CartItem(cart_id=user.cart.id, product_id=product_id, quantity=1)
                session.add(cart_item)

            session.commit()
            await query.answer(f"✅ {product.name} added to cart!")

    async def _show_product_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int):
        query = update.callback_query
        
        with Session() as session:
            product = session.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.answer("❌ Product not found")
                return

            text = f"💊 **{product.name}**\n\n"
            text += f"💰 **Price:** {self.format_price_with_usd(product.price_xmr)}\n\n"
            if product.description:
                text += f"📝 **Description:** {product.description}\n\n"
            
            text += "Available for one-time purchase."

            keyboard = [
                [InlineKeyboardButton("🛒 Add to Cart", callback_data=f"add_to_cart_{product.id}")],
                [InlineKeyboardButton("⬅️ Back to Products", callback_data="show_products")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def _start_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if not user or not user.cart or not user.cart.cart_items:
                await query.answer("❌ Your cart is empty")
                return

            # Set user state to start checkout process
            user_state = self.get_user_state(user_id)
            user_state.update({
                'checkout_flow': True,
                'current_step': 'full_name'
            })

            cart = user.cart
            total_amount = sum(item.product.price_xmr * item.quantity for item in cart.cart_items)

            await query.edit_message_text(
                f"🚀 **Proceeding to Checkout**\n\n"
                f"**Cart Total:** {self.format_price_with_usd(total_amount)}\n"
                f"**Items:** {len(cart.cart_items)}\n\n"
                "Please provide your shipping information:\n\n"
                "**Step 1 of 6: Full Name**\n"
                "Please enter your full name:",
                parse_mode="Markdown"
            )

    async def _collect_shipping_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        
        if not user_state.get('checkout_flow'):
            return

        current_step = user_state.get('current_step')
        text = update.message.text.strip()

        steps = {
            'full_name': {
                'field': 'full_name',
                'next_step': 'street_address',
                'prompt': "**Step 2 of 6: Street Address**\nPlease enter your street address:"
            },
            'street_address': {
                'field': 'street_address', 
                'next_step': 'apt_number',
                'prompt': "**Step 3 of 6: Apartment/Unit Number**\nPlease enter your apartment or unit number (or type 'none' if not applicable):"
            },
            'apt_number': {
                'field': 'apt_number',
                'next_step': 'city',
                'prompt': "**Step 4 of 6: City**\nPlease enter your city:"
            },
            'city': {
                'field': 'city',
                'next_step': 'state',
                'prompt': "**Step 5 of 6: State**\nPlease enter your state:"
            },
            'state': {
                'field': 'state',
                'next_step': 'zip_code', 
                'prompt': "**Step 6 of 6: ZIP Code**\nPlease enter your ZIP code:"
            },
            'zip_code': {
                'field': 'zip_code',
                'next_step': 'complete',
                'prompt': None
            }
        }

        if current_step in steps:
            user_state[steps[current_step]['field']] = text
            
            if steps[current_step]['next_step'] == 'complete':
                await self._create_order_from_cart(update, context)
            else:
                user_state['current_step'] = steps[current_step]['next_step']
                await update.message.reply_text(steps[steps[current_step]['next_step']]['prompt'], parse_mode="Markdown")

    async def _create_order_from_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        
        with Session() as session:
            user = session.query(User).filter(User.telegram_id == user_id).first()
            if not user or not user.cart or not user.cart.cart_items:
                await update.message.reply_text("❌ Error: Your cart is empty.")
                self.clear_user_state(user_id)
                return

            cart = user.cart
            
            # Calculate total and build order description
            total_amount = sum(item.product.price_xmr * item.quantity for item in cart.cart_items)
            item_count = len(cart.cart_items)
            order_description = f"Order with {item_count} item{'s' if item_count != 1 else ''}"

            # Create shipping address
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

            # Create payment request using advanced Monero handler
            payment_data = self.monero.create_payment_request(order_description, total_amount)
            if not payment_data:
                await update.message.reply_text("❌ Error generating payment. Please try again.")
                self.clear_user_state(user_id)
                return

            # Create order
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

            # Create order items
            for cart_item in cart.cart_items:
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=cart_item.product_id,
                    quantity=cart_item.quantity,
                    price_xmr=cart_item.product.price_xmr
                )
                session.add(order_item)

            # Clear cart after order creation
            session.delete(cart)
            session.commit()

            # Generate QR code for the payment request
            qr = qrcode.QRCode(version=1, box_size=8, border=4)
            qr.add_data(payment_data.get("payment_request"))
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, "PNG")
            bio.seek(0)

            # Build order summary
            shipping_summary = (
                f"**Shipping to:**\n"
                f"👤 {shipping_address.full_name}\n"
                f"🏠 {shipping_address.street_address}\n"
            )
            if shipping_address.apt_number:
                shipping_summary += f"🏢 Apt/Unit: {shipping_address.apt_number}\n"
            shipping_summary += f"📍 {shipping_address.city}, {shipping_address.state} {shipping_address.zip_code}"

            order_summary = "**Order Summary:**\n"
            for item in order.order_items:
                order_summary += f"• {item.product.name} × {item.quantity} = {self.format_price_with_usd(item.price_xmr * item.quantity)}\n"

            payment_text = (
                f"💰 **Payment Request**\n\n"
                f"{order_summary}\n"
                f"**Total Amount:** {self.format_price_with_usd(total_amount)}\n\n"
                f"{shipping_summary}\n\n"
                "**Instructions:**\n"
                "1️⃣ Scan the QR code or copy the payment request\n"
                "2️⃣ Use a Monero wallet that supports payment requests\n"
                "3️⃣ Click \"Check Payment\" after sending\n"
                "4️⃣ Your order will be shipped after confirmation\n\n"
                "⏰ **Payment expires in 30 minutes**"
            )

            keyboard = [
                [InlineKeyboardButton("🔍 Check Payment", callback_data=f"check_payment_{order.id}")],
                [InlineKeyboardButton("📋 Order Details", callback_data=f"order_details_{order.id}")],
                [InlineKeyboardButton("💊 Continue Shopping", callback_data="show_products")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_photo(
                photo=bio, 
                caption=payment_text, 
                reply_markup=reply_markup, 
                parse_mode="Markdown"
            )
            
            self.clear_user_state(user_id)

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
                await query.edit_message_text("❌ Payment expired. Please create a new order.")
                return

            # Use the enhanced payment checking
            payment_info = self.monero.check_payment(order.payment_id, order.total_amount_xmr)

            if payment_info:
                if payment_info.get("confirmations", 0) >= getattr(config, "CONFIRMATIONS_REQUIRED", 10):
                    order.status = "confirmed"
                    order.confirmed_at = datetime.utcnow()
                    
                    payment = Payment(
                        order_id=order.id,
                        tx_hash=payment_info.get("tx_hash"),
                        amount_xmr=float(payment_info.get("amount", 0.0)),
                        confirmations=payment_info.get("confirmations", 0),
                    )
                    session.add(payment)
                    session.commit()

                    # Build order items summary
                    items_summary = "**Order Items:**\n"
                    for item in order.order_items:
                        items_summary += f"• {item.product.name} × {item.quantity}\n"

                    shipping_info = ""
                    if order.shipping_address:
                        addr = order.shipping_address
                        shipping_info = (
                            f"\n**Shipping Address:**\n"
                            f"👤 {addr.full_name}\n"
                            f"🏠 {addr.street_address}\n"
                        )
                        if addr.apt_number:
                            shipping_info += f"🏢 {addr.apt_number}\n"
                        shipping_info += f"📍 {addr.city}, {addr.state} {addr.zip_code}"

                    delivery_text = (
                        f"✅ **Payment Confirmed!**\n\n"
                        f"**Order #** {order.id}\n"
                        f"**Transaction:** `{payment_info.get('tx_hash')}`\n"
                        f"**Confirmations:** {payment_info.get('confirmations')}\n"
                        f"**Amount:** {self.format_price_with_usd(payment_info.get('amount', 0.0))}\n"
                        f"{items_summary}\n"
                        f"{shipping_info}\n\n"
                        "Your order has been confirmed and will be shipped soon! 🎉\n"
                        "You will receive tracking information when available."
                    )
                    await query.edit_message_text(delivery_text, parse_mode="Markdown")
                else:
                    pending_text = (
                        f"⏳ **Payment Received - Pending Confirmation**\n\n"
                        f"**Order #** {order.id}\n"
                        f"**Amount:** {self.format_price_with_usd(payment_info.get('amount', 0.0))}\n"
                        f"**Transaction:** `{payment_info.get('tx_hash')}`\n"
                        f"**Confirmations:** {payment_info.get('confirmations', 0)}/{getattr(config, 'CONFIRMATIONS_REQUIRED', 10)}\n\n"
                        "Waiting for more confirmations..."
                    )
                    keyboard = [[InlineKeyboardButton("🔍 Check Again", callback_data=f"check_payment_{order.id}")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(pending_text, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await query.answer("❌ No payment received yet. Please send the exact amount to the address provided.")

    async def _show_order_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
        query = update.callback_query
        
        with Session() as session:
            order = session.query(Order).filter(Order.id == order_id).first()
            if not order:
                await query.answer("Order not found")
                return

            text = f"📋 **Order #{order.id}**\n\n"
            text += f"**Status:** {order.status.capitalize()}\n"
            text += f"**Total:** {self.format_price_with_usd(order.total_amount_xmr)}\n"
            text += f"**Created:** {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            text += f"**Expires:** {order.expires_at.strftime('%Y-%m-%d %H:%M')}\n\n"
            
            text += "**Items:**\n"
            for item in order.order_items:
                text += f"• {item.product.name} × {item.quantity} = {self.format_price_with_usd(item.price_xmr * item.quantity)}\n"
            
            text += f"\n**Shipping Address:**\n"
            if order.shipping_address:
                addr = order.shipping_address
                text += f"👤 {addr.full_name}\n"
                text += f"🏠 {addr.street_address}\n"
                if addr.apt_number:
                    text += f"🏢 {addr.apt_number}\n"
                text += f"📍 {addr.city}, {addr.state} {addr.zip_code}\n"

            keyboard = [
                [InlineKeyboardButton("🔍 Check Payment", callback_data=f"check_payment_{order.id}")],
                [InlineKeyboardButton("📋 All Orders", callback_data="my_orders")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def _show_products_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        self.clear_user_state(query.from_user.id)
        
        with Session() as session:
            products = session.query(Product).filter(Product.is_available == True).all()
            if not products:
                await query.edit_message_text("❌ No products available at the moment.")
                return

            text = "💊 **Available Products:**\n\n"
            keyboard = []
            for product in products:
                text += f"**{product.name}**\n💰 {self.format_price_with_usd(product.price_xmr)}\n"
                if product.description:
                    text += f"📝 {product.description}\n"
                text += "\n"
                keyboard.append([
                    InlineKeyboardButton(f"🛒 Add {product.name}", callback_data=f"add_to_cart_{product.id}"),
                    InlineKeyboardButton(f"ℹ️ Details", callback_data=f"product_details_{product.id}")
                ])

            keyboard.append([InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    async def _show_orders_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.show_orders(update, context)

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
                "Please use the menu buttons or commands to interact with the bot.\n\n"
                "Available commands:\n"
                "/start - Start the bot\n"
                "/products - Browse products\n" 
                "/cart - View your cart\n"
                "/orders - View your orders\n"
                "/clear_cart - Empty your cart\n"
                "/cancel - Cancel current operation"
            )

# Instantiate the bot
bot = MoneroBot()

def seed_products():
    with Session() as session:
        if session.query(Product).count() == 0:
            sample_products = [
                Product(
                    name="100ug Fluloprazolam Sheets", 
                    description="100ug sheets of Fluloprazolam - High quality research chemical",
                    price_xmr=0.0035,
                    is_available=True
                ),
                Product(
                    name="250ug Fluloprazolam Sheets", 
                    description="250ug sheets of Fluloprazolam - Premium research chemical",
                    price_xmr=0.0070,
                    is_available=True
                ),
                Product(
                    name="Dermorphin 5mg Vials", 
                    description="5mg vials of Dermorphin - Pharmaceutical grade",
                    price_xmr=0.0035,
                    is_available=True
                ),
                Product(
                    name="100ct Adderall", 
                    description="100 count Adderall tablets - Pharmaceutical grade",
                    price_xmr=0.0070,
                    is_available=True
                ),
                Product(
                    name="Tadalafil Powder 1g", 
                    description="1 gram Tadalafil powder - High purity",
                    price_xmr=0.0035,
                    is_available=True
                ),
                Product(
                    name="100mg Fluloprazolam Powder", 
                    description="100mg Fluloprazolam powder - Research chemical",
                    price_xmr=0.0140,
                    is_available=True
                ),
                Product(
                    name="Bromnordiazepam 1g", 
                    description="1 gram Bromnordiazepam powder - Research chemical",
                    price_xmr=0.0140,
                    is_available=True
                ),
                Product(
                    name="Promethazine Clearance 33.8g", 
                    description="33.8 grams Promethazine - Clearance sale",
                    price_xmr=0.0220,
                    is_available=True
                ),
            ]
            session.add_all(sample_products)
            session.commit()
            logger.info("✅ Seeded pharmaceutical products.")
        else:
            logger.info("✅ Products already exist.")

# -------------------------
# FastAPI Lifespan Events
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    seed_products()
    
    # ✅ INITIALIZE THE BOT PROPERLY
    await bot.application.initialize()
    await bot.application.start()
    
    # Set webhook for production
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await bot.application.bot.set_webhook(webhook_url)
        logger.info(f"✅ Webhook set to: {webhook_url}")
    else:
        logger.warning("No WEBHOOK_URL set - using getUpdates")
    
    logger.info("✅ Bot initialized and ready!")
    
    yield  # App runs here
    
    # Shutdown
    if bot.application.running:
        await bot.application.shutdown()
        await bot.application.stop()
    logger.info("Bot shutdown complete.")

# -------------------------
# FastAPI app
# -------------------------
app = FastAPI(title="Pharmacy Telegram Bot", lifespan=lifespan)

# -------------------------
# FastAPI Routes
# -------------------------
@app.get("/")
async def healthcheck():
    return {"status": "ok", "message": "Pharmacy Telegram Bot is running."}

@app.post("/webhook")
async def webhook(request: Request):
    """Handle incoming Telegram updates via webhook"""
    try:
        data = await request.json()
        logger.info(f"📨 WEBHOOK RECEIVED - Update ID: {data.get('update_id')}")
        
        update = Update.de_json(data, bot.application.bot)
        
        if update.message:
            logger.info(f"💬 Message from {update.effective_user.id}: {update.message.text}")
        elif update.callback_query:
            logger.info(f"🔘 Callback from {update.effective_user.id}: {update.callback_query.data}")
        
        await bot.application.process_update(update)
        logger.info("✅ Update processed successfully")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

# For local development with polling
def main():
    seed_products()
    logger.info("Starting bot with polling...")
    
    async def run_bot():
        await bot.application.initialize()
        await bot.application.start()
        await bot.application.updater.start_polling()
        
        logger.info("Bot is now running...")
        
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)
    
    asyncio.run(run_bot())

if __name__ == "__main__":
    # If running locally, use polling. If deployed, use webhooks.
    if os.getenv("RENDER") or os.getenv("WEBHOOK_URL"):
        # This will be used in deployment
        pass
    else:
        main()