import os
from dotenv import load_dotenv

load_dotenv()

# Bot Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]

# Monero Configuration
MONERO_RPC_URL = os.getenv('MONERO_RPC_URL')
MONERO_WALLET_RPC_URL = os.getenv('MONERO_WALLET_RPC_URL')
MONERO_WALLET_PASSWORD = os.getenv('MONERO_WALLET_PASSWORD', '')

# Database
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///bot.db')

# Bot Settings
PAYMENT_TIMEOUT = 1800  # 30 minutes in seconds
CONFIRMATIONS_REQUIRED = 10