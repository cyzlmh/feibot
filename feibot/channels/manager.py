"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from feibot.bus.events import OutboundMessage
from feibot.bus.queue import MessageBus
from feibot.channels.feishu import FeishuChannel
from feibot.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.
    
    Responsibilities:
    - Initialize enabled channels (Feishu only)
    - Start/stop channels
    - Route outbound messages
    """
    
    def __init__(self, config: Config, bus: MessageBus, workspace_dir: Path | None = None):
        self.config = config
        self.bus = bus
        self.workspace_dir = workspace_dir
        self.feishu: FeishuChannel | None = None
        self._dispatch_task: asyncio.Task | None = None
        
        self._init_channels()
    
    def _init_channels(self) -> None:
        """Initialize channels based on config."""

        if not self.config.channels.feishu.enabled:
            return

        self.feishu = FeishuChannel(
            self.config.channels.feishu,
            self.bus,
            workspace_dir=self.workspace_dir,
        )
        logger.info("Feishu channel enabled")
    
    async def _start_channel(self, channel: FeishuChannel) -> None:
        """Start Feishu channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error(f"Failed to start Feishu channel: {e}")

    async def start_all(self) -> None:
        """Start Feishu channel and the outbound dispatcher."""
        if self.feishu is None:
            logger.warning("Feishu channel is not enabled")
            return
        
        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        logger.info("Starting feishu channel...")
        await self._start_channel(self.feishu)
    
    async def stop_all(self) -> None:
        """Stop Feishu channel and the dispatcher."""
        logger.info("Stopping channel manager...")
        
        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        
        # Stop Feishu channel
        if self.feishu is not None:
            try:
                await self.feishu.stop()
                logger.info("Stopped feishu channel")
            except Exception as e:
                logger.error(f"Error stopping feishu: {e}")
    
    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to Feishu."""
        logger.info("Outbound dispatcher started")
        
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                if msg.channel != "feishu":
                    logger.debug(f"Skipping outbound message for non-Feishu channel: {msg.channel}")
                    continue

                if self.feishu is None:
                    logger.warning("Dropping Feishu outbound message: channel is disabled")
                    continue

                try:
                    await self.feishu.send(msg)
                except Exception as e:
                    logger.error(f"Error sending to Feishu: {e}")
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
    
    def get_channel(self, name: str) -> FeishuChannel | None:
        """Get a channel by name."""
        return self.feishu if name == "feishu" else None
    
    def get_status(self) -> dict[str, Any]:
        """Get status of the Feishu channel."""
        if self.feishu is None:
            return {}
        return {"feishu": {"enabled": True, "running": self.feishu.is_running}}
    
    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return ["feishu"] if self.feishu is not None else []
