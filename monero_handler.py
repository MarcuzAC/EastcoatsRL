import requests
import json
import time
import logging
from decimal import Decimal
from datetime import datetime
from typing import Dict, Optional, Any
import config
from database import Session, Order, Payment

# Import your Monero libraries
from monerorequest import (
    make_random_payment_id, 
    convert_datetime_object_to_truncated_RFC3339_timestamp_format,
    decode_monero_payment_request, 
    make_monero_payment_request
)

logger = logging.getLogger(__name__)

class MoneroHandler:
    def __init__(self):
        self.rpc_url = config.MONERO_RPC_URL
        self.wallet_rpc_url = config.MONERO_WALLET_RPC_URL
        
    def rpc_call(self, method, params=None, wallet_rpc=False):
        """Make RPC call to Monero daemon or wallet"""
        url = self.wallet_rpc_url if wallet_rpc else self.rpc_url
        
        if not url:
            logger.error("Monero RPC URL not configured")
            return None
            
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
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            response.raise_for_status()
            result = response.json()
            
            if 'error' in result and result['error']:
                logger.error(f"Monero RPC error: {result['error']}")
                return None
                
            return result.get('result')
        except requests.exceptions.RequestException as e:
            logger.error(f"Monero RPC request failed: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Monero RPC JSON decode failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in RPC call: {e}")
            return None

    def create_payment_request(self, order_description: str, total_amount_xmr: float) -> Dict[str, Any]:
        """Create a Monero payment request for one-time purchase"""
        try:
            # Generate payment ID
            payment_id = make_random_payment_id()
            
            # Create integrated address
            address_result = self.rpc_call(
                "make_integrated_address", 
                {"payment_id": payment_id},
                wallet_rpc=True
            )
            
            if not address_result:
                raise Exception("Failed to create integrated address")
                
            integrated_address = address_result.get("integrated_address")
            
            # Create payment request using your library
            payment_request = make_monero_payment_request(
                custom_label=order_description,
                sellers_wallet=integrated_address,
                currency="XMR",
                amount=str(total_amount_xmr),
                payment_id=payment_id,
                start_date=convert_datetime_object_to_truncated_RFC3339_timestamp_format(datetime.now()),
                number_of_payments=1,  # One-time payment only
                version="2",  # Use V2 for better features
                allow_standard=True,
                allow_integrated_address=True,
                allow_subaddress=False,
                allow_stagenet=False
            )
            
            return {
                "payment_request": payment_request,
                "integrated_address": integrated_address,
                "payment_id": payment_id,
                "amount_xmr": total_amount_xmr,
                "expires_at": datetime.now().timestamp() + config.PAYMENT_TIMEOUT
            }
            
        except Exception as e:
            logger.error(f"Error creating payment request: {e}")
            return None

    def decode_payment_request(self, payment_request_code: str) -> Optional[Dict]:
        """Decode a Monero payment request"""
        try:
            # Handle the prefix inconsistency between encode/decode
            if payment_request_code.startswith("monero-request:"):
                payment_request_code = payment_request_code.replace("monero-request:", "monero:")
            
            return decode_monero_payment_request(payment_request_code)
        except Exception as e:
            logger.error(f"Error decoding payment request: {e}")
            return None

    def check_payment(self, payment_id: str, expected_amount: float) -> Optional[Dict[str, Any]]:
        """Check for payments using payment ID"""
        try:
            # Get wallet height for confirmation calculation
            wallet_height_result = self.rpc_call("get_height", wallet_rpc=True)
            if not wallet_height_result:
                return None
                
            wallet_height = wallet_height_result.get("height", 0)
            
            # Method 1: Check using get_payments (more reliable for payment IDs)
            payments_result = self.rpc_call(
                "get_payments", 
                {"payment_id": payment_id},
                wallet_rpc=True
            )
            
            if payments_result and 'payments' in payments_result:
                for payment in payments_result['payments']:
                    amount_xmr = Decimal(payment.get('amount', 0)) / Decimal(1e12)
                    tx_hash = payment.get('tx_hash')
                    block_height = payment.get('block_height', 0)
                    
                    if amount_xmr >= Decimal(expected_amount) - Decimal('0.000001'):  # Allow small rounding
                        confirmations = max(0, wallet_height - block_height) if block_height > 0 else 0
                        
                        return {
                            "tx_hash": tx_hash,
                            "amount": float(amount_xmr),
                            "confirmations": confirmations,
                            "block_height": block_height,
                            "payment_id": payment_id
                        }
            
            # Method 2: Fallback to get_transfers (for older wallet versions)
            transfers_result = self.rpc_call("get_transfers", {
                "in": True,
                "pending": True,
                "failed": False
            }, wallet_rpc=True)
            
            if transfers_result and 'in' in transfers_result:
                for transfer in transfers_result['in']:
                    # Extract payment ID from address if possible
                    if (transfer.get('address') and payment_id in transfer.get('address', '') and
                        Decimal(transfer['amount']) / Decimal(1e12) >= Decimal(expected_amount)):
                        
                        confirmations = transfer.get('confirmations', 0)
                        return {
                            'tx_hash': transfer.get('txid'),
                            'amount': float(Decimal(transfer['amount']) / Decimal(1e12)),
                            'confirmations': confirmations,
                            'payment_id': payment_id
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking Monero payment: {e}")
            return None

    def create_address(self, order_id):
        """Create a new payment address for an order (backward compatibility)"""
        try:
            # Use the new payment request system but return simple address
            payment_data = self.create_payment_request(
                order_description=f"Order #{order_id}",
                total_amount_xmr=0.0  # Amount will be set by caller
            )
            
            if payment_data:
                return {
                    'integrated_address': payment_data['integrated_address'],
                    'payment_id': payment_data['payment_id']
                }
        except Exception as e:
            logger.error(f"Error creating address: {e}")
        return None

    def get_balance(self):
        """Get wallet balance"""
        try:
            result = self.rpc_call("get_balance", wallet_rpc=True)
            if result:
                return {
                    'balance': float(Decimal(result['balance']) / Decimal(1e12)),
                    'unlocked_balance': float(Decimal(result['unlocked_balance']) / Decimal(1e12))
                }
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
        return None

    def validate_address(self, address):
        """Validate Monero address"""
        try:
            result = self.rpc_call("validate_address", {"address": address}, wallet_rpc=True)
            return result.get('valid') if result else False
        except Exception as e:
            logger.error(f"Error validating address: {e}")
        return False

    def verify_payment_complete(self, order_id: int) -> bool:
        """Verify if payment is complete and update database"""
        try:
            with Session() as session:
                order = session.query(Order).filter(Order.id == order_id).first()
                if not order:
                    logger.error(f"Order {order_id} not found")
                    return False

                # Check payment status
                payment_info = self.check_payment(order.payment_id, order.total_amount_xmr)
                
                if payment_info:
                    if payment_info.get("confirmations", 0) >= getattr(config, "CONFIRMATIONS_REQUIRED", 10):
                        # Update order status
                        if order.status != 'confirmed':
                            order.status = 'confirmed'
                            order.confirmed_at = datetime.utcnow()
                        
                        # Create or update payment record
                        existing_payment = session.query(Payment).filter(
                            Payment.order_id == order.id
                        ).first()
                        
                        if not existing_payment:
                            payment = Payment(
                                order_id=order.id,
                                tx_hash=payment_info.get("tx_hash"),
                                amount_xmr=payment_info.get("amount", 0.0),
                                confirmations=payment_info.get("confirmations", 0),
                            )
                            session.add(payment)
                        
                        session.commit()
                        logger.info(f"Payment confirmed for order {order_id}")
                        return True
                    else:
                        # Payment received but waiting for confirmations
                        logger.info(f"Payment pending confirmations for order {order_id}")
                        return False
                else:
                    # Check if payment expired
                    if datetime.utcnow() > order.expires_at and order.status == 'pending':
                        order.status = 'expired'
                        session.commit()
                        logger.info(f"Order {order_id} expired")
                    
                    return False
                    
        except Exception as e:
            logger.error(f"Error verifying payment for order {order_id}: {e}")
            return False

    def get_payment_status(self, order_id: int) -> Dict[str, Any]:
        """Get detailed payment status for an order"""
        try:
            with Session() as session:
                order = session.query(Order).filter(Order.id == order_id).first()
                if not order:
                    return {"error": "Order not found"}
                
                payment_info = self.check_payment(order.payment_id, order.total_amount_xmr)
                
                status_info = {
                    "order_id": order_id,
                    "status": order.status,
                    "expected_amount": order.total_amount_xmr,
                    "payment_id": order.payment_id,
                    "expires_at": order.expires_at.isoformat(),
                    "is_expired": datetime.utcnow() > order.expires_at
                }
                
                if payment_info:
                    status_info.update({
                        "payment_received": True,
                        "amount_received": payment_info.get("amount", 0.0),
                        "confirmations": payment_info.get("confirmations", 0),
                        "confirmations_required": getattr(config, "CONFIRMATIONS_REQUIRED", 10),
                        "tx_hash": payment_info.get("tx_hash"),
                        "is_confirmed": payment_info.get("confirmations", 0) >= getattr(config, "CONFIRMATIONS_REQUIRED", 10)
                    })
                else:
                    status_info.update({
                        "payment_received": False,
                        "amount_received": 0.0,
                        "confirmations": 0,
                        "is_confirmed": False
                    })
                
                return status_info
                
        except Exception as e:
            logger.error(f"Error getting payment status for order {order_id}: {e}")
            return {"error": str(e)}