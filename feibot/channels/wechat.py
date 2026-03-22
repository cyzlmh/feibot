"""WeChat channel implementation using ilink API (OpenClaw-compatible).

This uses the same ilink API as OpenClaw's official WeChat plugin.
No special authorization required - just scan QR code to login.

Reference: @tencent-weixin/openclaw-weixin (MIT licensed)
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from feibot.bus.events import OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.base import BaseChannel
from feibot.config.schema import WeChatConfig


class WeChatChannel(BaseChannel):
    """
    WeChat channel using ilink API.
    
    Uses long-poll getUpdates to receive messages.
    No public IP or webhook required.
    
    Requires:
    - Scan QR code to login (bot_type=3)
    - bot_token and ilink_bot_id stored after login
    """
    
    name = "wechat"
    
    # API endpoints (relative to api_base_url)
    ENDPOINTS = {
        "get_bot_qrcode": "ilink/bot/get_bot_qrcode",
        "get_qrcode_status": "ilink/bot/get_qrcode_status",
        "getupdates": "ilink/bot/getupdates",
        "sendmessage": "ilink/bot/sendmessage",
        "getuploadurl": "ilink/bot/getuploadurl",
        "getconfig": "ilink/bot/getconfig",
        "sendtyping": "ilink/bot/sendtyping",
    }
    
    # Timeouts
    LONG_POLL_TIMEOUT_MS = 35000  # 35s for long-poll
    API_TIMEOUT_SEC = 15.0
    
    def __init__(self, config: WeChatConfig, bus: MessageBus, state_dir: Path | None = None):
        super().__init__(config, bus)
        self.config: WeChatConfig = config
        self.state_dir = (state_dir or Path.home() / ".feibot" / "wechat").expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        self._client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._get_updates_buf: str = ""  # Sync cursor for long-poll
        self._context_tokens: dict[str, str] = {}  # user_id -> context_token for replying
    
    async def start(self) -> None:
        """Start the WeChat channel."""
        if not self.config.enabled:
            logger.info("WeChat channel is disabled")
            return
        
        # Check if we have credentials
        if not self.config.bot_token:
            logger.warning("WeChat bot_token not configured. Please login first.")
            logger.info("Run: feibot wechat login")
            return
        
        self._running = True
        
        # Create HTTP client
        self._client = httpx.AsyncClient(
            base_url=self.config.api_base_url,
            timeout=httpx.Timeout(self.API_TIMEOUT_SEC),
        )
        
        # Start long-poll for messages
        self._poll_task = asyncio.create_task(self._poll_messages())
        
        logger.info(f"WeChat channel started (bot_id: {self.config.ilink_bot_id})")
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the WeChat channel."""
        self._running = False
        
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        
        if self._client:
            await self._client.aclose()
            self._client = None
        
        logger.info("WeChat channel stopped")
    
    def _build_headers(self) -> dict[str, str]:
        """Build common headers for API requests."""
        # X-WECHAT-UIN: random uint32 -> base64
        uin = random.randint(0, 2**32 - 1)
        uin_b64 = base64.b64encode(str(uin).encode()).decode()
        
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.config.bot_token}",
            "X-WECHAT-UIN": uin_b64,
        }
    
    async def _api_call(
        self, 
        endpoint: str, 
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Make an API call to ilink."""
        if not self._client:
            raise RuntimeError("WeChat client not initialized")
        
        url = self.ENDPOINTS.get(endpoint, endpoint)
        headers = self._build_headers()
        
        try:
            if json_body:
                response = await self._client.post(
                    url, 
                    headers=headers, 
                    json=json_body,
                    timeout=timeout or self.API_TIMEOUT_SEC,
                )
            elif params:
                response = await self._client.get(
                    url, 
                    headers=headers, 
                    params=params,
                    timeout=timeout or self.API_TIMEOUT_SEC,
                )
            else:
                response = await self._client.get(
                    url, 
                    headers=headers,
                    timeout=timeout or self.API_TIMEOUT_SEC,
                )
            
            response.raise_for_status()
            data = response.json()
            
            if data.get("ret") != 0:
                errcode = data.get("errcode", -1)
                errmsg = data.get("errmsg", "unknown error")
                logger.error(f"WeChat API error: {errcode} - {errmsg}")
                return {"ret": -1, "errcode": errcode, "errmsg": errmsg}
            
            return data
            
        except httpx.HTTPStatusError as e:
            logger.error(f"WeChat API HTTP error: {e}")
            return {"ret": -1, "errmsg": str(e)}
        except Exception as e:
            logger.error(f"WeChat API error: {e}")
            return {"ret": -1, "errmsg": str(e)}
    
    async def _poll_messages(self) -> None:
        """Long-poll for incoming messages."""
        logger.info("WeChat message polling started")
        
        while self._running:
            try:
                # Build request body
                body = {
                    "get_updates_buf": self._get_updates_buf,
                    "base_info": {"channel_version": "1.0.0"},
                }
                
                # Use longer timeout for long-poll
                data = await self._api_call(
                    "getupdates", 
                    json_body=body,
                    timeout=40.0,  # Slightly longer than server timeout
                )
                
                if data.get("ret") != 0:
                    errcode = data.get("errcode", 0)
                    if errcode == -14:  # Session expired
                        logger.error("WeChat session expired. Please re-login.")
                        self._running = False
                        break
                    await asyncio.sleep(1)
                    continue
                
                # Update sync cursor
                self._get_updates_buf = data.get("get_updates_buf", "")
                
                # Process messages
                msgs = data.get("msgs", [])
                for msg in msgs:
                    await self._handle_incoming_message(msg)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error polling WeChat messages: {e}")
                await asyncio.sleep(5)
        
        logger.info("WeChat message polling stopped")
    
    async def _handle_incoming_message(self, msg: dict[str, Any]) -> None:
        """Handle an incoming WeChat message."""
        try:
            from_user = msg.get("from_user_id", "")
            to_user = msg.get("to_user_id", "")  # Bot's ID
            msg_type = msg.get("msg_type", 0)
            context_token = msg.get("context_token", "")
            
            # Log the sender for allow_from configuration
            logger.info(f"WeChat message from user_id=[yellow]{from_user}[/yellow]")
            
            # Store context token for replying
            if from_user and context_token:
                self._context_tokens[from_user] = context_token
            
            # Save contact info for status command
            self._save_contact(from_user, msg)
            
            # Extract content based on message type
            content = ""
            media: list[str] = []
            
            if msg_type == 1:  # Text message
                content = msg.get("text", "")
            elif msg_type == 3:  # Image message
                content = "[图片]"
                img_url = msg.get("thumb_url") or msg.get("cdn_url")
                if img_url:
                    media.append(img_url)
            elif msg_type == 34:  # Voice message
                content = "[语音]"
            elif msg_type == 43:  # Video message
                content = "[视频]"
            elif msg_type == 49:  # Rich media/file
                content = msg.get("title", "[文件]")
            else:
                content = f"[消息类型:{msg_type}]"
            
            if not content:
                return
            
            # Handle the message through base channel
            await self._handle_message(
                sender_id=from_user,
                chat_id=from_user,  # Private chat, so chat_id = user_id
                content=content,
                media=media if media else None,
                metadata={
                    "msg_type": msg_type,
                    "context_token": context_token,
                    "to_user": to_user,
                },
            )
            
        except Exception as e:
            logger.error(f"Error handling WeChat message: {e}")
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WeChat."""
        if not self._client:
            logger.warning("WeChat client not initialized")
            return
        
        user_id = msg.chat_id
        context_token = self._context_tokens.get(user_id, "")
        
        if not context_token:
            logger.warning(f"No context_token for user {user_id}, cannot send")
            return
        
        # Build message payload
        body = {
            "msg": {
                "to_user_id": user_id,
                "context_token": context_token,
                "item_list": [
                    {
                        "type": 1,  # Text type
                        "text_item": {
                            "text": msg.content,
                        },
                    }
                ],
            },
            "base_info": {"channel_version": "1.0.0"},
        }
        
        data = await self._api_call("sendmessage", json_body=body)
        
        if data.get("ret") == 0:
            logger.debug(f"Sent WeChat message to user {user_id}")
        else:
            logger.error(f"Failed to send WeChat message: {data.get('errmsg')}")
    
    # ==================== Login Methods ====================
    
    async def login(self) -> bool:
        """
        Perform QR code login.
        
        Returns:
            True if login successful, False otherwise.
        """
        logger.info("Starting WeChat login...")
        
        async with httpx.AsyncClient(
            base_url=self.config.api_base_url,
            timeout=httpx.Timeout(self.API_TIMEOUT_SEC),
        ) as client:
            # Step 1: Get QR code
            try:
                response = await client.get(
                    self.ENDPOINTS["get_bot_qrcode"],
                    params={"bot_type": self.config.bot_type},
                )
                data = response.json()
            except Exception as e:
                logger.error(f"Failed to get QR code: {e}")
                return False
            
            if data.get("ret") != 0:
                logger.error(f"QR API error: {data.get('errmsg', 'unknown')}")
                return False
            
            qrcode = data.get("qrcode", "")
            qr_url = data.get("qrcode_img_content", "")
            
            if not qrcode:
                logger.error("No qrcode in response")
                return False
            
            # Display QR code
            self._display_qr(qr_url or qrcode)
            
            # Step 2: Poll for scan status
            logger.info("Waiting for QR code scan...")
            
            for _ in range(60):  # Wait up to 5 minutes
                await asyncio.sleep(5)
                try:
                    response = await client.get(
                        self.ENDPOINTS["get_qrcode_status"],
                        params={"qrcode": qrcode},
                    )
                    status_data = response.json()
                except Exception as e:
                    logger.warning(f"Error polling QR status: {e}")
                    continue
                
                status = status_data.get("status", "")
                
                if status == "scaned":
                    logger.info("QR code scanned! Waiting for confirmation...")
                elif status == "confirmed":
                    bot_token = status_data.get("bot_token", "")
                    ilink_bot_id = status_data.get("ilink_bot_id", "")
                    
                    if bot_token and ilink_bot_id:
                        logger.info(f"Login successful! Bot ID: {ilink_bot_id}")
                        
                        # Save credentials
                        self.config.bot_token = bot_token
                        self.config.ilink_bot_id = ilink_bot_id
                        self._save_credentials(bot_token, ilink_bot_id)
                        
                        return True
                    else:
                        logger.error("Login confirmed but missing credentials")
                        return False
                elif status == "expired":
                    logger.error("QR code expired. Please try again.")
                    return False
            
            logger.error("Login timeout. Please try again.")
            return False
    
    def _display_qr(self, qr_content: str) -> None:
        """Display QR code for scanning."""
        print("\n" + "=" * 50)
        print("Scan this QR code with WeChat:")
        print("=" * 50 + "\n")
        
        # If it's a URL, print it
        if qr_content.startswith("http"):
            print(f"URL: {qr_content}\n")
        
        # Generate ASCII QR code
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(qr_content)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"QR content: {qr_content}")
            print("Tip: Install 'qrcode' package for ASCII QR display")
        
        print("\n" + "=" * 50 + "\n")
    
    def _save_credentials(self, bot_token: str, ilink_bot_id: str) -> None:
        """Save credentials to state file."""
        cred_file = self.state_dir / "credentials.json"
        data = {
            "bot_token": bot_token,
            "ilink_bot_id": ilink_bot_id,
            "bot_type": self.config.bot_type,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        with open(cred_file, "w") as f:
            json.dump(data, f, indent=2)
        
        logger.info(f"Credentials saved to {cred_file}")
    
    def load_credentials(self) -> bool:
        """Load credentials from state file."""
        cred_file = self.state_dir / "credentials.json"
        
        if not cred_file.exists():
            return False
        
        try:
            with open(cred_file) as f:
                data = json.load(f)
            
            self.config.bot_token = data.get("bot_token", "")
            self.config.ilink_bot_id = data.get("ilink_bot_id", "")
            
            return bool(self.config.bot_token and self.config.ilink_bot_id)
        except Exception as e:
            logger.error(f"Error loading credentials: {e}")
            return False
    
    def _save_contact(self, user_id: str, msg: dict[str, Any]) -> None:
        """Save contact info for allow_from configuration."""
        contacts_file = self.state_dir / "contacts.json"
        
        # Load existing contacts
        contacts = {}
        if contacts_file.exists():
            try:
                with open(contacts_file) as f:
                    contacts = json.load(f)
            except Exception:
                pass
        
        # Update contact info
        if user_id and user_id not in contacts:
            contacts[user_id] = {
                "first_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                "nickname": msg.get("nickname", ""),
            }
            
            with open(contacts_file, "w") as f:
                json.dump(contacts, f, indent=2)
    
    @staticmethod
    def get_contacts() -> dict[str, Any]:
        """Get saved contacts (for CLI status command)."""
        contacts_file = Path.home() / ".feibot" / "wechat" / "contacts.json"
        
        if not contacts_file.exists():
            return {}
        
        try:
            with open(contacts_file) as f:
                return json.load(f)
        except Exception:
            return {}