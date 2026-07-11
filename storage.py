"""Хранилище файлов вложений лида — байты на диске, метаданные в ``LeadAttachment``.

Транспорт — data-URI в JSON (тот же паттерн, что и логотип продавца в sales:
клиент кодирует файл через FileReader, сервер multipart не парсит — его в
проекте нет и лишнюю зависимость ради этого тянуть незачем). В отличие от
логотипа (маленький, хранится в БД как строка) — вложения могут весить
несколько МБ, поэтому байты пишутся на диск атомарно (см. паттерн скачивания
записей звонков в ``connectors/bitrix.py``: tmp-файл + ``os.replace``).
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import uuid
from pathlib import Path

# Разрешённые типы вложений заявки: документы + сканы. Список сознательно
# короткий — расширять по запросу, не «на всякий случай».
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 МБ — скан/xlsx с запасом, не видеофайл
# Потолок длины base64-строки под MAX_SIZE_BYTES: base64 раздувает данные в ~4/3.
# Нужен, чтобы отсечь огромный data_url ДО b64decode (которое материализует всю
# строку в память) — иначе злонамеренный/случайный мегабайтный ввод проедает RAM
# до проверки размера в save_attachment.
_MAX_B64_CHARS = (MAX_SIZE_BYTES // 3 + 1) * 4

_DATA_DIR = Path(os.getenv("AIOS_LEADS_DATA_DIR", "./data/leads/attachments"))
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentRejected(ValueError):
    """Вложение не прошло проверку границы доверия (тип/размер/формат)."""


def _sanitize_filename(filename: str) -> str:
    name = Path(filename).name.strip() or "file"
    return _SAFE_NAME_RE.sub("_", name)[:120]


def decode_data_url(data_url: str) -> tuple[str, bytes]:
    """``data:<mime>;base64,<...>`` → (mime, байты). Кидает ``AttachmentRejected``."""
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_url, re.DOTALL)
    if not m:
        raise AttachmentRejected("Ожидался data-URI вида data:<mime>;base64,<...>")
    content_type, b64 = m.group(1), m.group(2)
    if len(b64) > _MAX_B64_CHARS:
        raise AttachmentRejected(f"Файл больше {MAX_SIZE_BYTES // (1024 * 1024)} МБ")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentRejected("Некорректный base64") from exc
    return content_type, data


def save_attachment(lead_id: int, filename: str, content_type: str, data: bytes) -> tuple[str, int]:
    """Провалидировать (тип/размер) и атомарно записать файл на диск.

    Возвращает ``(storage_path, size_bytes)``; ``storage_path`` — относительный
    путь (от ``_DATA_DIR`), чтобы в БД не утекал абсолютный путь машины.
    """
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise AttachmentRejected(f"Тип файла не поддерживается: {content_type}")
    size = len(data)
    if size == 0:
        raise AttachmentRejected("Пустой файл")
    if size > MAX_SIZE_BYTES:
        raise AttachmentRejected(f"Файл больше {MAX_SIZE_BYTES // (1024 * 1024)} МБ")

    ext = ALLOWED_CONTENT_TYPES[content_type]
    safe_name = _sanitize_filename(filename)
    unique = f"{uuid.uuid4().hex}_{safe_name}"
    if not unique.endswith(ext):
        unique = f"{unique}{ext}"

    lead_dir = _DATA_DIR / str(lead_id)
    lead_dir.mkdir(parents=True, exist_ok=True)
    final_path = lead_dir / unique
    tmp_path = final_path.with_suffix(final_path.suffix + ".part")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, final_path)

    storage_path = str(Path(str(lead_id)) / unique)
    return storage_path, size


def _resolve_within_data_dir(storage_path: str) -> Path:
    """Абсолютный путь вложения с гардом обхода каталога (path traversal)."""
    path = (_DATA_DIR / storage_path).resolve()
    if _DATA_DIR.resolve() not in path.parents:
        raise AttachmentRejected("Некорректный путь вложения")
    return path


def read_attachment(storage_path: str) -> bytes:
    """Прочитать байты вложения по относительному пути из ``LeadAttachment.storage_path``."""
    return _resolve_within_data_dir(storage_path).read_bytes()


def delete_attachment(storage_path: str) -> None:
    """Удалить файл вложения с диска (идемпотентно: отсутствующий — не ошибка).

    Ошибочно загруженный файл с ПДн (скан не того клиента) должен быть удаляем,
    а не жить на диске вечно. Гард обхода каталога — тот же, что при чтении.
    """
    _resolve_within_data_dir(storage_path).unlink(missing_ok=True)
