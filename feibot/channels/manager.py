"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from feibot.bus.queue import MessageBus
from feibot.channels.feishu import FeishuChannel
from feibot.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.
    
    Responsibilities:
    - Initialize enabled channels (Feishu)
    - Start/stop channels
    - Route outbound messages to correct channels
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

        # Init Feishu channel
        if self.config.channels.feishu.enabled:
            self.feishu = FeishuChannel(
                self.config.channels.feishu,
                self.bus,
                workspace_dir=self.workspace_dir,
            )
            logger.info("Feishu channel enabled")
    
    async def _start_channel(self, channel: FeishuChannel) -> None:
        """Start a channel and log any exceptions."""
        channel_name = channel.name
        try:
            await channel.start()
        except Exception as e:
            logger.error(f"Failed to start {channel_name} channel: {e}")

    async def start_all(self) -> None:
        """Start all enabled channels and the outbound dispatcher."""
        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        
        # Start all channels in parallel (each runs its own loop)
        tasks = []
        
        if self.feishu is not None:
            logger.info("Starting Feishu channel...")
            tasks.append(asyncio.create_task(self._start_channel(self.feishu)))
        
        if not tasks:
            logger.warning("No channels are enabled")
        
        # Wait for all channels (they run indefinitely until stopped)
        if tasks:
            await asyncio.gather(*tasks)
    
    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping channel manager...")
        
        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        
        # Stop Feishu
        if self.feishu is not None:
            try:
                await self.feishu.stop()
                logger.info("Stopped Feishu channel")
            except Exception as e:
                logger.error(f"Error stopping Feishu: {e}")
    
    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to appropriate channels."""
        logger.info("Outbound dispatcher started")
        
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )

                # Handle progress messages
                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                # Route to correct channel
                channel = self.get_channel(msg.channel)
                if channel is None:
                    logger.warning(f"Dropping outbound message: channel '{msg.channel}' not available")
                    continue

                try:
                    await channel.send(msg)
                except Exception as e:
                    logger.error(f"Error sending to {msg.channel}: {e}")
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
        
        logger.info("Outbound dispatcher stopped")
    
    def get_channel(self, name: str) -> FeishuChannel | None:
        """Get a channel by name."""
        if name == "feishu":
            return self.feishu
        return None
    
    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        status = {}
        if self.feishu is not None:
            status["feishu"] = {"enabled": True, "running": self.feishu.is_running}
        return status
    
    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        channels = []
        if self.feishu is not None:
            channels.append("feishu")
        return channels
