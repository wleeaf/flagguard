"""Tests for attachment pre-download size checking."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from handlers._helpers import _download_file


@pytest.mark.anyio
async def test_oversized_file_rejected_before_download():
    """A file exceeding MAX_ATTACHMENT_BYTES should raise ValueError before downloading."""
    bot = AsyncMock()
    tg_file = MagicMock()
    tg_file.file_size = 100 * 1024 * 1024  # 100MB, way over the 12MB limit
    tg_file.file_path = "photos/file.jpg"
    bot.get_file.return_value = tg_file

    with pytest.raises(ValueError, match="File too large"):
        await _download_file(bot, "file_id_123")

    # download_file should NOT have been called
    bot.download_file.assert_not_called()


@pytest.mark.anyio
async def test_normal_sized_file_downloads_successfully():
    """A file within limits should download normally."""
    bot = AsyncMock()
    tg_file = MagicMock()
    tg_file.file_size = 1024  # 1KB
    tg_file.file_path = "photos/file.jpg"
    bot.get_file.return_value = tg_file

    import io
    buf = io.BytesIO(b"file content")

    async def mock_download(path, destination):
        destination.write(b"file content")

    bot.download_file.side_effect = mock_download

    data = await _download_file(bot, "file_id_123")
    assert data == b"file content"
    bot.download_file.assert_called_once()


@pytest.mark.anyio
async def test_file_with_no_size_metadata_still_works():
    """If file_size is None (not provided by Telegram), download should proceed."""
    bot = AsyncMock()
    tg_file = MagicMock()
    tg_file.file_size = None
    tg_file.file_path = "photos/file.jpg"
    bot.get_file.return_value = tg_file

    async def mock_download(path, destination):
        destination.write(b"data")

    bot.download_file.side_effect = mock_download

    data = await _download_file(bot, "file_id_123")
    assert data == b"data"
