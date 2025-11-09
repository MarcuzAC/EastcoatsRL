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
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    orders = relationship("Order", back_populates="user")

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)
    price_xmr = Column(Float, nullable=False)
    image_url = Column(String)
    is_available = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    orders = relationship("Order", back_populates="product")

class ShippingAddress(Base):
    __tablename__ = 'shipping_addresses'
    id = Column(Integer, primary_key=True)
    full_name = Column(String, nullable=False)
    street_address = Column(String, nullable=False)
    apt_number = Column(String)  # Can be null
    city = Column(String, nullable=False)
    state = Column(String, nullable=False)
    zip_code = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    orders = relationship("Order", back_populates="shipping_address")

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    shipping_address_id = Column(Integer, ForeignKey('shipping_addresses.id'), nullable=False)
    amount_xmr = Column(Float, nullable=False)
    payment_address = Column(String, nullable=False)
    payment_id = Column(String)
    status = Column(String, default='pending')  # pending, paid, confirmed, shipped, completed, expired
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    
    # Relationships
    user = relationship("User", back_populates="orders")
    product = relationship("Product", back_populates="orders")
    shipping_address = relationship("ShippingAddress", back_populates="orders")
    payments = relationship("Payment", back_populates="order")

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False)
    tx_hash = Column(String)
    amount_xmr = Column(Float, nullable=False)
    confirmations = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    order = relationship("Order", back_populates="payments")

# Create tables
Base.metadata.create_all(engine)