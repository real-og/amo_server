import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

load_dotenv()

app = FastAPI(title="M-TRANS Drupal → amoCRM")

# amoCRM настройки.
AMO_BASE_URL = os.getenv("AMO_BASE_URL", "").rstrip("/")
AMO_ACCESS_TOKEN = os.getenv("AMO_ACCESS_TOKEN", "")
AMO_PIPELINE_ID = os.getenv("AMO_PIPELINE_ID", "")

# Название источника в amoCRM.
SOURCE_NAME = os.getenv("SOURCE_NAME", "M-TRANS.by")

# Добавлять ли примечание со всеми полями формы.
ADD_NOTE_TO_LEAD = os.getenv("ADD_NOTE_TO_LEAD", "true").lower() == "true"

# Логи.
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def save_incoming_payload(data: dict[str, Any]) -> None:
    """
    Сохраняем каждую входящую заявку в файл.

    Это важно: даже если amoCRM вернёт ошибку,
    у нас останется сырая заявка от Drupal.
    """
    with open(LOG_DIR / "incoming.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def normalize_text(value: Any) -> str:
    """
    Безопасно превращает любое значение в строку.
    """
    if value is None:
        return ""

    return str(value).strip()


def get_fields(data: dict[str, Any]) -> dict[str, Any]:
    """
    Возвращает fields из Drupal payload.
    """
    fields = data.get("fields")

    if isinstance(fields, dict):
        return fields

    return {}


def get_field_by_key(data: dict[str, Any], keys: list[str]) -> str:
    """
    Ищет поле по точному machine key.

    Например:
    imya, telefon, e_mail, tekst.
    """
    fields = get_fields(data)

    for key in keys:
        field = fields.get(key)

        if isinstance(field, dict):
            value = normalize_text(field.get("value"))

            if value:
                return value

    return ""


def extract_contact(data: dict[str, Any]) -> dict[str, str]:
    """
    Достаёт контакт из твоих известных форм.

    Твои формы:
    13 - imya, e_mail, telefon
    18 - tekst, imya, telefon
    35 - imya, telefon
    36 - imya, telefon
    37 - imya, telefon
    """
    name = get_field_by_key(data, ["imya"])
    phone = get_field_by_key(data, ["telefon"])
    email = get_field_by_key(data, ["e_mail", "email", "mail"])

    return {
        "name": name or "Без имени",
        "phone": phone,
        "email": email,
    }


def extract_message(data: dict[str, Any]) -> str:
    """
    Достаёт текст/комментарий из формы.

    Сейчас точно знаем поле tekst у формы 18.
    """
    return get_field_by_key(data, ["tekst", "message", "comment", "soobshchenie"])


def build_note_text(data: dict[str, Any]) -> str:
    """
    Собирает текст примечания для amoCRM.

    В это примечание складываем вообще все поля формы,
    чтобы менеджер видел исходную заявку полностью.
    """
    lines = [
        "Заявка с сайта M-TRANS.by",
        "",
        f"Форма: {data.get('form_title', '')}",
        f"NID формы: {data.get('form_nid', '')}",
        f"Submission ID: {data.get('submission_id', '')}",
        f"Страница: {data.get('page', '')}",
        f"IP: {data.get('ip', '')}",
        f"Время: {data.get('submitted_at', '')}",
        "",
        "Поля формы:",
    ]

    fields = get_fields(data)

    for field in fields.values():
        if not isinstance(field, dict):
            continue

        label = field.get("label") or field.get("key") or "Поле"
        value = field.get("value") or ""

        lines.append(f"- {label}: {value}")

        file_urls = field.get("file_urls") or []

        if isinstance(file_urls, list):
            for url in file_urls:
                lines.append(f"  Файл: {url}")

    message = extract_message(data)

    if message:
        lines.extend([
            "",
            "Комментарий:",
            message,
        ])

    return "\n".join(lines)


def build_amo_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Собирает payload для amoCRM /api/v4/leads/unsorted/forms.
    """
    contact = extract_contact(data)

    contact_custom_fields = []

    # Телефон контакта.
    if contact["phone"]:
        contact_custom_fields.append({
            "field_code": "PHONE",
            "values": [
                {
                    "value": contact["phone"],
                }
            ],
        })

    # Email контакта.
    if contact["email"]:
        contact_custom_fields.append({
            "field_code": "EMAIL",
            "values": [
                {
                    "value": contact["email"],
                }
            ],
        })

    contact_payload: dict[str, Any] = {
        "name": contact["name"],
    }

    if contact_custom_fields:
        contact_payload["custom_fields_values"] = contact_custom_fields

    form_nid = str(data.get("form_nid", "unknown"))
    submission_id = str(data.get("submission_id", "unknown"))
    form_title = normalize_text(data.get("form_title")) or "Форма сайта"

    # Уникальный UID заявки.
    # Нужен amoCRM для идентификации источника заявки.
    source_uid = f"mtrans_webform_{form_nid}_{submission_id}"

    created_at = data.get("submitted_at_unix")

    if not isinstance(created_at, int):
        created_at = int(time.time())

    lead_name = f"Заявка с сайта M-TRANS.by — {form_title}"

    lead_payload: dict[str, Any] = {
        "name": lead_name,
    }

    item: dict[str, Any] = {
        "request_id": source_uid,
        "source_name": SOURCE_NAME,
        "source_uid": source_uid,
        "created_at": created_at,

        # metadata обязательна для неразобранного типа forms.
        "metadata": {
            "form_id": form_nid,
            "form_name": form_title,
            "form_page": normalize_text(data.get("page")),
            "ip": normalize_text(data.get("ip")),
            "form_sent_at": str(created_at),
            "referer": normalize_text(data.get("page")),
        },

        "_embedded": {
            "leads": [
                lead_payload,
            ],
            "contacts": [
                contact_payload,
            ],
        },
    }

    if AMO_PIPELINE_ID:
        item["pipeline_id"] = int(AMO_PIPELINE_ID)

    return [item]


async def amo_request(method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
    """
    Универсальный запрос в amoCRM.
    """
    if not AMO_BASE_URL:
        raise RuntimeError("AMO_BASE_URL is empty")

    if not AMO_ACCESS_TOKEN:
        raise RuntimeError("AMO_ACCESS_TOKEN is empty")

    url = f"{AMO_BASE_URL}{path}"

    headers = {
        "Authorization": f"Bearer {AMO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            json=payload,
        )

    if response.status_code < 200 or response.status_code >= 300:
        logging.error("amoCRM error %s: %s", response.status_code, response.text)

        raise HTTPException(
            status_code=502,
            detail={
                "message": "amoCRM error",
                "status_code": response.status_code,
                "response": response.text,
            },
        )

    if not response.text:
        return {}

    return response.json()


def extract_lead_id_from_unsorted_response(response: dict[str, Any]) -> int | None:
    """
    Пытается достать ID сделки из ответа amoCRM.
    """
    try:
        unsorted = response["_embedded"]["unsorted"][0]
        lead = unsorted["_embedded"]["leads"][0]
        return int(lead["id"])
    except Exception:
        return None


async def try_add_note_to_lead(lead_id: int, text: str) -> dict[str, Any] | None:
    """
    Добавляет обычное текстовое примечание к сделке.

    Если примечание не добавилось — не валим всю заявку.
    Сама заявка в неразобранном важнее.
    """
    payload = [
        {
            "note_type": "common",
            "params": {
                "text": text,
            },
        }
    ]

    try:
        return await amo_request("POST", f"/api/v4/leads/{lead_id}/notes", payload)
    except Exception as exc:
        logging.exception("Failed to add note to lead %s: %s", lead_id, exc)
        return None


@app.get("/health")
async def health():
    """
    Проверка, что FastAPI жив.
    """
    return {
        "ok": True,
        "time": datetime.now().isoformat(),
    }


@app.post("/drupal/webform")
async def receive_drupal_webform(request: Request):
    """
    Один endpoint для всех Drupal Webform-заявок.
    """
    raw_body = await request.body()
    print(raw_body)

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # save_incoming_payload(data)

    # logging.info(
    #     "Incoming Drupal webform: form_nid=%s submission_id=%s",
    #     data.get("form_nid"),
    #     data.get("submission_id"),
    # )

    # amo_payload = build_amo_payload(data)

    # amo_response = await amo_request(
    #     "POST",
    #     "/api/v4/leads/unsorted/forms",
    #     amo_payload,
    # )

    # note_response = None

    # if ADD_NOTE_TO_LEAD:
    #     lead_id = extract_lead_id_from_unsorted_response(amo_response)

    #     if lead_id:
    #         note_text = build_note_text(data)
    #         note_response = await try_add_note_to_lead(lead_id, note_text)
    #     else:
    #         logging.warning("Lead ID was not found in amoCRM unsorted response")

    # return {
    #     "ok": True,
    #     "message": "Webhook received and sent to amoCRM",
    #     "form_nid": data.get("form_nid"),
    #     "submission_id": data.get("submission_id"),
    #     "amo_response": amo_response,
    #     "note_response": note_response,
    # }
    return {"ok": True,}