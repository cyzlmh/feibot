"""WeChat channel implementation using ilink API (OpenClaw-compatible).

This uses the same ilink API as OpenClaw's official WeChat plugin.
No special authorization required - just scan QR code to login.

Reference: @tencent-weixin/openclaw-weixin (MIT licensed)
Reference: nanobot PR #2348 (weixin.py)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from feibot.bus.events import OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.base import BaseChannel
from feibot.config.schema import WeChatConfig


# ---------------------------------------------------------------------------
# Protocol constants (from openclaw-weixin types.ts)
# ---------------------------------------------------------------------------

# MessageItemType
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# MessageType (1 = inbound from user, 2 = outbound from bot)
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2

# MessageState
MESSAGE_STATE_FINISH = 2

# Max message length before splitting
WECHAT_MAX_MESSAGE_LEN = 4000

# Base info for API calls
BASE_INFO: dict[str, str] = {"channel_version": "1.0.2"}

# Session-expired error code
ERRCODE_SESSION_EXPIRED = -14

# Retry constants
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2

# Default long-poll timeout
DEFAULT_LONG_POLL_TIMEOUT_S = 35

# CDN base URL for media download
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


# ---------------------------------------------------------------------------
# AES-128-ECB decryption (from pic-decrypt.ts)
# ---------------------------------------------------------------------------

def _parse_aes_key(aes_key_b64: str) -> bytes:
    """Parse a base64-encoded AES key.
    
    Handles two encodings:
    - base64(raw 16 bytes) -> images
    - base64(hex string of 16 bytes) -> file/voice/video
    """
    raw = base64.b64decode(aes_key_b64)
    # If 32 bytes, it's hex-encoded
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    return raw


def _decrypt_aes_ecb(data: bytes, aes_key_b64: str) -> bytes:
    """Decrypt AES-128-ECB encrypted media."""
    try:
        from Crypto.Cipher import AES
        key = _parse_aes_key(aes_key_b64)
        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.decrypt(data)
    except ImportError:
        logger.warning("pycryptodome not installed, cannot decrypt media")
        return data
    except Exception as e:
        logger.error(f"AES decryption error: {e}")
        return data


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
        self._processed_ids: OrderedDict[str, None] = OrderedDict()  # Dedup
        self._consecutive_failures: int = 0  # For backoff
    
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
        """Build common headers for API requests (new UIN each call)."""
        # X-WECHAT-UIN: random uint32 -> base64 (matches reference plugin)
        uin = int.from_bytes(os.urandom(4), "big")
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
                # Add base_info if not present
                if "base_info" not in json_body:
                    json_body["base_info"] = BASE_INFO
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
            
            logger.debug(f"WeChat API response: {data}")
            
            # Check for errors (use errcode, not ret)
            errcode = data.get("errcode", 0)
            if errcode and errcode != 0:
                errmsg = data.get("errmsg", "unknown error")
                logger.error(f"WeChat API error: errcode={errcode}, errmsg={errmsg}")
                return {"errcode": errcode, "errmsg": errmsg}
            
            return data
            
        except httpx.HTTPStatusError as e:
            logger.error(f"WeChat API HTTP error: {e}")
            return {"errcode": -1, "errmsg": str(e)}
        except Exception as e:
            logger.error(f"WeChat API error: {e}")
            return {"errcode": -1, "errmsg": str(e)}
    
    async def _poll_messages(self) -> None:
        """Long-poll for incoming messages."""
        logger.info("WeChat message polling started")
        
        while self._running:
            try:
                # Backoff if too many consecutive failures
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        f"WeChat: {self._consecutive_failures} consecutive failures, "
                        f"backing off for {BACKOFF_DELAY_S}s"
                    )
                    await asyncio.sleep(BACKOFF_DELAY_S)
                    self._consecutive_failures = 0
                
                # Build request body
                body = {
                    "get_updates_buf": self._get_updates_buf,
                    "base_info": BASE_INFO,
                }
                
                # Use longer timeout for long-poll
                data = await self._api_call(
                    "getupdates", 
                    json_body=body,
                    timeout=40.0,  # Slightly longer than server timeout
                )
                
                # Check for errors using errcode
                errcode = data.get("errcode", 0)
                if errcode and errcode != 0:
                    if errcode == ERRCODE_SESSION_EXPIRED:  # Session expired
                        logger.error("WeChat session expired. Please re-login.")
                        self._running = False
                        break
                    self._consecutive_failures += 1
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue
                
                # Success - reset failure counter
                self._consecutive_failures = 0
                
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
                self._consecutive_failures += 1
                await asyncio.sleep(RETRY_DELAY_S)
        
        logger.info("WeChat message polling stopped")
    
    async def _handle_incoming_message(self, msg: dict[str, Any]) -> None:
        """Handle an incoming WeChat message using item_list format."""
        try:
            msg_id = str(msg.get("message_id", ""))
            from_user = msg.get("from_user_id", "")
            to_user = msg.get("to_user_id", "")  # Bot's ID
            context_token = msg.get("context_token", "")
            
            # Dedup
            if msg_id in self._processed_ids:
                return
            self._processed_ids[msg_id] = None
            if len(self._processed_ids) > 1000:
                self._processed_ids.popitem(last=False)
            
            # Log the sender for allow_from configuration
            logger.info(f"WeChat message from user_id=[yellow]{from_user}[/yellow]")
            
            # Store context token for replying
            if from_user and context_token:
                self._context_tokens[from_user] = context_token
            
            # Check allow_from
            if self.config.allow_from and from_user not in self.config.allow_from:
                logger.debug(f"Ignoring message from unauthorized user: {from_user}")
                return
            
            # Save contact info for status command
            self._save_contact(from_user, msg)
            
            # Parse item_list (new format from ilink API)
            item_list: list[dict] = msg.get("item_list") or []
            content_parts: list[str] = []
            media_paths: list[str] = []
            
            for item in item_list:
                item_type = item.get("type", 0)
                
                if item_type == ITEM_TEXT:
                    text = (item.get("text_item") or {}).get("text", "")
                    if text:
                        # Handle quoted messages
                        ref = item.get("ref_msg")
                        if ref:
                            ref_item = ref.get("message_item")
                            if ref_item and ref_item.get("type", 0) in (
                                ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO
                            ):
                                content_parts.append(text)
                            else:
                                parts: list[str] = []
                                if ref.get("title"):
                                    parts.append(ref["title"])
                                if ref_item:
                                    ref_text = (ref_item.get("text_item") or {}).get("text", "")
                                    if ref_text:
                                        parts.append(ref_text)
                                if parts:
                                    content_parts.append(f"[引用: {' | '.join(parts)}]\n{text}")
                                else:
                                    content_parts.append(text)
                        else:
                            content_parts.append(text)
                
                elif item_type == ITEM_IMAGE:
                    image_item = item.get("image_item") or {}
                    file_path = await self._download_media_item(image_item, "image")
                    if file_path:
                        content_parts.append(f"[图片]\n[Image: {file_path}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append("[图片]")
                
                elif item_type == ITEM_VOICE:
                    voice_item = item.get("voice_item") or {}
                    voice_text = voice_item.get("text", "")  # WeChat voice-to-text
                    if voice_text:
                        content_parts.append(f"[语音] {voice_text}")
                    else:
                        file_path = await self._download_media_item(voice_item, "voice")
                        if file_path:
                            content_parts.append(f"[语音]\n[Audio: {file_path}]")
                            media_paths.append(file_path)
                        else:
                            content_parts.append("[语音]")
                
                elif item_type == ITEM_FILE:
                    file_item = item.get("file_item") or {}
                    file_name = file_item.get("file_name", "unknown")
                    file_path = await self._download_media_item(file_item, "file", file_name)
                    if file_path:
                        content_parts.append(f"[文件: {file_name}]\n[File: {file_path}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append(f"[文件: {file_name}]")
                
                elif item_type == ITEM_VIDEO:
                    video_item = item.get("video_item") or {}
                    file_path = await self._download_media_item(video_item, "video")
                    if file_path:
                        content_parts.append(f"[视频]\n[Video: {file_path}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append("[视频]")
            
            content = "\n".join(content_parts)
            if not content:
                return
            
            logger.info(f"WeChat inbound: items={','.join(str(i.get('type', 0)) for i in item_list)}")
            
            # Handle the message through base channel
            await self._handle_message(
                sender_id=from_user,
                chat_id=from_user,  # Private chat, so chat_id = user_id
                content=content,
                media=media_paths or None,
                metadata={"message_id": msg_id},
            )
            
        except Exception as e:
            logger.error(f"Error handling WeChat message: {e}")
    
    async def _download_media_item(
        self,
        typed_item: dict,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """Download and decrypt a media item. Returns local path or None."""
        try:
            media = typed_item.get("media") or {}
            encrypt_query_param = media.get("encrypt_query_param", "")
            
            if not encrypt_query_param:
                return None
            
            # Resolve AES key
            # image_item.aeskey is hex string (32 chars = 16 bytes)
            # media.aes_key is base64-encoded
            raw_aeskey_hex = typed_item.get("aeskey", "")
            media_aes_key_b64 = media.get("aes_key", "")
            
            aes_key_b64: str = ""
            if raw_aeskey_hex:
                # Convert hex -> raw bytes -> base64
                aes_key_b64 = base64.b64encode(bytes.fromhex(raw_aeskey_hex)).decode()
            elif media_aes_key_b64:
                aes_key_b64 = media_aes_key_b64
            
            # Build CDN download URL
            cdn_url = f"{CDN_BASE_URL}/download?encrypted_query_param={quote(encrypt_query_param)}"
            
            assert self._client is not None
            resp = await self._client.get(cdn_url)
            resp.raise_for_status()
            data = resp.content
            
            # Decrypt if we have AES key
            if aes_key_b64 and data:
                data = _decrypt_aes_ecb(data, aes_key_b64)
            elif not aes_key_b64:
                logger.debug(f"No AES key for {media_type} item, using raw bytes")
            
            if not data:
                return None
            
            # Save to media directory
            media_dir = self.state_dir / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate filename
            ext = {"image": ".jpg", "voice": ".mp3", "video": ".mp4", "file": ""}.get(media_type, "")
            if not filename:
                filename = f"{media_type}_{uuid.uuid4().hex[:8]}{ext}"
            
            file_path = media_dir / filename
            file_path.write_bytes(data)
            
            logger.debug(f"Downloaded {media_type} to {file_path}")
            return str(file_path)
            
        except Exception as e:
            logger.error(f"Error downloading {media_type}: {e}")
            return None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WeChat."""
        if not self._client:
            logger.warning("WeChat client not initialized")
            return
        
        content = msg.content.strip()
        if not content:
            return
        
        user_id = msg.chat_id
        context_token = self._context_tokens.get(user_id, "")
        
        if not context_token:
            logger.warning(f"No context_token for user {user_id}, cannot send")
            return
        
        # Split message if too long (4000 chars max)
        chunks = self._split_message(content, WECHAT_MAX_MESSAGE_LEN)
        
        for chunk in chunks:
            await self._send_text(user_id, chunk, context_token)
    
    def _split_message(self, text: str, max_len: int) -> list[str]:
        """Split message into chunks respecting max length."""
        if len(text) <= max_len:
            return [text]
        
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try to split at newline or space
            split_pos = max_len
            for i in range(max_len - 1, max(0, max_len - 100), -1):
                if text[i] in '\n ':
                    split_pos = i + 1
                    break
            chunks.append(text[:split_pos])
            text = text[split_pos:]
        return chunks
    
    async def _send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> None:
        """Send a text message matching the exact protocol from nanobot PR #2348."""
        client_id = f"feibot-{uuid.uuid4().hex[:12]}"
        
        item_list: list[dict] = []
        if text:
            item_list.append({"type": ITEM_TEXT, "text_item": {"text": text}})
        
        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
        }
        if item_list:
            weixin_msg["item_list"] = item_list
        if context_token:
            weixin_msg["context_token"] = context_token
        
        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": BASE_INFO,
        }
        
        data = await self._api_call("sendmessage", json_body=body)
        
        # Check for errors using errcode
        errcode = data.get("errcode", 0)
        if errcode and errcode != 0:
            logger.warning(f"WeChat send error (code {errcode}): {data.get('errmsg')}")
        else:
            logger.debug(f"Sent WeChat message to user {to_user_id}")
    
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