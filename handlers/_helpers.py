"""Shared helpers used by multiple handler modules."""

import asyncio
import io
import logging
import mimetypes
from functools import partial

from aiogram import Bot, types

from config import MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, MAX_TOTAL_ATTACHMENT_BYTES

# Limit concurrent attachment downloads to cap memory pressure
_download_semaphore = asyncio.Semaphore(20)


async def _download_file(bot: Bot, file_id: str, *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> bytes:
    async with _download_semaphore:
        tg_file = await bot.get_file(file_id)
        if tg_file.file_size and tg_file.file_size > max_bytes:
            raise ValueError(
                f"File too large ({tg_file.file_size} bytes, max {max_bytes})"
            )
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        data = buf.getvalue()
        if len(data) > max_bytes:
            raise ValueError(
                f"File too large after download ({len(data)} bytes, max {max_bytes})"
            )
        return data


def _guess_mime(filename: str | None, fallback: str = "application/octet-stream") -> str:
    if not filename:
        return fallback
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


async def extract_attachments(bot: Bot, message: types.Message) -> list[dict]:
    """Extract supported attachments from a Telegram message."""
    attachments = []
    total_bytes = 0

    def _remaining_bytes() -> int:
        return max(0, MAX_TOTAL_ATTACHMENT_BYTES - total_bytes)

    def _at_limit() -> bool:
        return len(attachments) >= MAX_ATTACHMENTS

    if message.photo and not _at_limit():
        largest = message.photo[-1]
        try:
            remaining = _remaining_bytes()
            if remaining <= 0:
                logging.warning("Attachment budget exhausted; skipping photo")
            else:
                data = await _download_file(
                    bot, largest.file_id, max_bytes=min(MAX_ATTACHMENT_BYTES, remaining)
                )
                total_bytes += len(data)
                attachments.append({"kind": "photo", "filename": "photo.jpg", "mime": "image/jpeg", "bytes": data, "size": len(data)})
        except ValueError as e:
            logging.warning("Skipping oversized photo: %s", e)

    if message.document and not _at_limit():
        doc = message.document
        try:
            remaining = _remaining_bytes()
            if remaining <= 0:
                logging.warning("Attachment budget exhausted; skipping document")
            else:
                data = await _download_file(
                    bot, doc.file_id, max_bytes=min(MAX_ATTACHMENT_BYTES, remaining)
                )
                total_bytes += len(data)
                attachments.append({"kind": "document", "filename": doc.file_name or "document", "mime": doc.mime_type or _guess_mime(doc.file_name), "bytes": data, "size": len(data)})
        except ValueError as e:
            logging.warning("Skipping oversized document: %s", e)

    if message.video and not _at_limit():
        try:
            remaining = _remaining_bytes()
            if remaining <= 0:
                logging.warning("Attachment budget exhausted; skipping video")
            else:
                data = await _download_file(
                    bot, message.video.file_id, max_bytes=min(MAX_ATTACHMENT_BYTES, remaining)
                )
                total_bytes += len(data)
                attachments.append({"kind": "video", "filename": "video.mp4", "mime": "video/mp4", "bytes": data, "size": len(data)})
        except ValueError as e:
            logging.warning("Skipping oversized video: %s", e)

    if message.animation and not _at_limit():
        anim = message.animation
        try:
            remaining = _remaining_bytes()
            if remaining <= 0:
                logging.warning("Attachment budget exhausted; skipping animation")
            else:
                data = await _download_file(
                    bot, anim.file_id, max_bytes=min(MAX_ATTACHMENT_BYTES, remaining)
                )
                total_bytes += len(data)
                attachments.append({"kind": "animation", "filename": anim.file_name or "animation.gif", "mime": anim.mime_type or "image/gif", "bytes": data, "size": len(data)})
        except ValueError as e:
            logging.warning("Skipping oversized animation: %s", e)

    if message.audio and not _at_limit():
        aud = message.audio
        try:
            remaining = _remaining_bytes()
            if remaining <= 0:
                logging.warning("Attachment budget exhausted; skipping audio")
            else:
                data = await _download_file(
                    bot, aud.file_id, max_bytes=min(MAX_ATTACHMENT_BYTES, remaining)
                )
                total_bytes += len(data)
                attachments.append({"kind": "audio", "filename": aud.file_name or "audio", "mime": aud.mime_type or _guess_mime(aud.file_name, "audio/mpeg"), "bytes": data, "size": len(data)})
        except ValueError as e:
            logging.warning("Skipping oversized audio: %s", e)

    if message.voice and not _at_limit():
        try:
            remaining = _remaining_bytes()
            if remaining <= 0:
                logging.warning("Attachment budget exhausted; skipping voice")
            else:
                data = await _download_file(
                    bot, message.voice.file_id, max_bytes=min(MAX_ATTACHMENT_BYTES, remaining)
                )
                total_bytes += len(data)
                attachments.append({"kind": "voice", "filename": "voice.ogg", "mime": "audio/ogg", "bytes": data, "size": len(data)})
        except ValueError as e:
            logging.warning("Skipping oversized voice: %s", e)

    return attachments


def safe_text(message: types.Message) -> str:
    return message.text or message.caption or ""


_RUN_BLOCKING_TIMEOUT: float = 30.0


async def run_blocking(func, *args, **kwargs):
    """Run blocking repository/database work off the event loop with a timeout."""
    if kwargs:
        coro = asyncio.to_thread(partial(func, *args, **kwargs))
    else:
        coro = asyncio.to_thread(func, *args)
    return await asyncio.wait_for(coro, timeout=_RUN_BLOCKING_TIMEOUT)
