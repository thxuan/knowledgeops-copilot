"""Bounded upload validation; durable intake runs outside the event loop."""

import asyncio
import hashlib
from pathlib import PurePosixPath

from fastapi import UploadFile
from app.core.errors import AppError
from app.core.identity import Identity
from app.db.ingestion import IngestionRepository
from app.ingestion.parser import PARSER_VERSION


class DocumentService:
    def __init__(self, max_bytes: int, repository: IngestionRepository) -> None:
        self.max_bytes = max_bytes
        self.repository = repository

    async def read_upload(self, file: UploadFile):
        filename = PurePosixPath((file.filename or "").replace("\\", "/")).name
        extension = filename.lower().rsplit(".", 1)[-1]
        if not filename or extension not in {"md", "txt"}:
            raise AppError("UNSUPPORTED_FILE", "Only .md and .txt documents are accepted", 415)
        content = await file.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise AppError("DOCUMENT_TOO_LARGE", "Document exceeds the configured size limit", 413)
        if not content:
            raise AppError("EMPTY_DOCUMENT", "Document is empty", 422)

        return filename, extension, content

    async def receive(self, file: UploadFile, identity: Identity,
                      source: str | None = None, version: str | None = None) -> dict:
        filename, extension, content = await self.read_upload(file)

        def persist():
            return self.repository.submit(
                identity=identity, filename=filename, content=content,
                file_hash=hashlib.sha256(content).hexdigest(),
                parser_version=f"{PARSER_VERSION}:{extension}",
                source=source or filename, version=version,
            )

        return await asyncio.to_thread(persist)

