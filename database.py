from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import config

Base = declarative_base()
engine = create_engine(config.DATABASE_URL)
Session = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    last_name = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    orders = relationship("Order", back_populates="user")
    cart = relationship("Cart", back_populates="user", uselist=False, cascade="all, delete-orphan")

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    price_xmr = Column(Float, nullable=False)
    image_url = Column(String(500))
    is_available = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    cart_items = relationship("CartItem", back_populates="product")
    order_items = relationship("OrderItem", back_populates="product")

class ShippingAddress(Base):
    __tablename__ = 'shipping_addresses'
    id = Column(Integer, primary_key=True)
    full_name = Column(String(255), nullable=False)
    street_address = Column(String(500), nullable=False)
    apt_number = Column(String(100))
    city = Column(String(100), nullable=False)
    state = Column(String(100), nullable=False)
    zip_code = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    orders = relationship("Order", back_populates="shipping_address")

class Cart(Base):
    __tablename__ = 'carts'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="cart")
    cart_items = relationship("CartItem", back_populates="cart", cascade="all, delete-orphan")

class CartItem(Base):
    __tablename__ = 'cart_items'
    id = Column(Integer, primary_key=True)
    cart_id = Column(Integer, ForeignKey('carts.id'), nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    cart = relationship("Cart", back_populates="cart_items")
    product = relationship("Product", back_populates="cart_items")

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    shipping_address_id = Column(Integer, ForeignKey('shipping_addresses.id'), nullable=False)
    total_amount_xmr = Column(Float, nullable=False)
    payment_address = Column(String(255), nullable=False)
    payment_id = Column(String(255))
    payment_request = Column(Text)
    status = Column(String(50), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    
    # Relationships
    user = relationship("User", back_populates="orders")
    shipping_address = relationship("ShippingAddress", back_populates="orders")
    payments = relationship("Payment", back_populates="order")
    order_items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = 'order_items'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_xmr = Column(Float, nullable=False)
    
    # Relationships
    order = relationship("Order", back_populates="order_items")
    product = relationship("Product", back_populates="order_items")

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False)
    tx_hash = Column(String(255))
    amount_xmr = Column(Float, nullable=False)
    confirmations = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    order = relationship("Order", back_populates="payments")

# Create tables
def init_db():
    Base.metadata.create_all(engine)

# Initialize database
init_db()