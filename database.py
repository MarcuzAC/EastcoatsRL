from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
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

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)
    price_xmr = Column(Float, nullable=False)
    image_url = Column(String)
    is_available = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    product_id = Column(Integer, nullable=False)
    amount_xmr = Column(Float, nullable=False)
    payment_address = Column(String, nullable=False)
    payment_id = Column(String)
    status = Column(String, default='pending')  # pending, paid, confirmed, completed, expired
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime)
    confirmed_at = Column(DateTime)

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, nullable=False)
    tx_hash = Column(String)
    amount_xmr = Column(Float, nullable=False)
    confirmations = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(engine)