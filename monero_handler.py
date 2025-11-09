import requests
import json
import time
from decimal import Decimal
import config
from database import Session, Order, Payment

class MoneroHandler:
    def __init__(self):
        self.rpc_url = config.MONERO_RPC_URL
        self.wallet_rpc_url = config.MONERO_WALLET_RPC_URL
        
    def rpc_call(self, method, params=None, wallet_rpc=False):
        url = self.wallet_rpc_url if wallet_rpc else self.rpc_url
        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {}
        }
        
        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json'}
            )
            result = response.json()
            return result.get('result')
        except Exception as e:
            print(f"RPC Error: {e}")
            return None

    def create_address(self, order_id):
        """Create a new payment address for an order"""
        try:
            # Generate integrated address with payment ID
            result = self.rpc_call("make_integrated_address", wallet_rpc=True)
            if result:
                return {
                    'integrated_address': result['integrated_address'],
                    'payment_id': result['payment_id']
                }
        except Exception as e:
            print(f"Error creating address: {e}")
        return None

    def check_payment(self, payment_address, expected_amount):
        """Check if payment has been received"""
        try:
            # Get transactions
            result = self.rpc_call("get_transfers", {
                "in": True,
                "pending": True,
                "failed": False
            }, wallet_rpc=True)
            
            if result and 'in' in result:
                for transfer in result['in']:
                    if (transfer['address'] == payment_address and 
                        Decimal(transfer['amount']) / 1e12 >= Decimal(expected_amount)):
                        return {
                            'tx_hash': transfer['txid'],
                            'amount': Decimal(transfer['amount']) / 1e12,
                            'confirmations': transfer.get('confirmations', 0)
                        }
        except Exception as e:
            print(f"Error checking payment: {e}")
        return None

    def get_balance(self):
        """Get wallet balance"""
        try:
            result = self.rpc_call("get_balance", wallet_rpc=True)
            if result:
                return {
                    'balance': Decimal(result['balance']) / 1e12,
                    'unlocked_balance': Decimal(result['unlocked_balance']) / 1e12
                }
        except Exception as e:
            print(f"Error getting balance: {e}")
        return None

    def validate_address(self, address):
        """Validate Monero address"""
        try:
            result = self.rpc_call("validate_address", {"address": address}, wallet_rpc=True)
            return result.get('valid') if result else False
        except Exception as e:
            print(f"Error validating address: {e}")
        return False