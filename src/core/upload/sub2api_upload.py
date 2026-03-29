"""
Sub2API 账号上传能力

未配置目标分组时，继续沿用 sub2api-data 导入模式。
配置了目标分组时，改为按账号查找后更新/创建，并在请求中携带 group_ids。
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from ...database.session import get_db

logger = logging.getLogger(__name__)


def _build_account_credentials(acc: Account) -> dict:
    expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else 0
    return {
        "access_token": acc.access_token,
        "chatgpt_account_id": acc.account_id or "",
        "chatgpt_user_id": "",
        "client_id": acc.client_id or "",
        "expires_at": expires_at,
        "expires_in": 863999,
        "model_mapping": {
            "gpt-5.1": "gpt-5.1",
            "gpt-5.1-codex": "gpt-5.1-codex",
            "gpt-5.1-codex-max": "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
            "gpt-5.2": "gpt-5.2",
            "gpt-5.2-codex": "gpt-5.2-codex",
            "gpt-5.3": "gpt-5.3",
            "gpt-5.3-codex": "gpt-5.3-codex",
            "gpt-5.4": "gpt-5.4",
        },
        "organization_id": acc.workspace_id or "",
        "refresh_token": acc.refresh_token or "",
    }


def _build_admin_headers(api_key: str, idempotency_key: Optional[str] = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _extract_response_data(response) -> object:
    payload = response.json()
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _extract_error_message(response, default_message: str) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error") or payload.get("detail")
            if detail:
                return str(detail)
    except Exception:
        pass

    text = (getattr(response, "text", "") or "").strip()
    if text:
        return f"{default_message} - {text[:200]}"
    return default_message


def _build_grouped_account_create_payload(
    acc: Account,
    target_group_ids: List[int],
    concurrency: int,
    priority: int,
) -> dict:
    expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else None
    payload = {
        "name": acc.email,
        "platform": "openai",
        "type": "oauth",
        "credentials": _build_account_credentials(acc),
        "extra": {},
        "concurrency": concurrency,
        "priority": priority,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
        "group_ids": list(target_group_ids),
    }
    if expires_at:
        payload["expires_at"] = expires_at
    return payload


def _build_grouped_account_update_payload(
    acc: Account,
    target_group_ids: List[int],
    concurrency: int,
    priority: int,
) -> dict:
    expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else None
    payload = {
        "name": acc.email,
        "type": "oauth",
        "credentials": _build_account_credentials(acc),
        "extra": {},
        "concurrency": concurrency,
        "priority": priority,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
        "group_ids": list(target_group_ids),
    }
    if expires_at:
        payload["expires_at"] = expires_at
    return payload


def _build_import_account_item(acc: Account, concurrency: int, priority: int) -> dict:
    return {
        "name": acc.email,
        "platform": "openai",
        "type": "oauth",
        "credentials": _build_account_credentials(acc),
        "extra": {},
        "concurrency": concurrency,
        "priority": priority,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def _find_existing_sub2api_account(api_url: str, api_key: str, account_name: str) -> Optional[dict]:
    url = api_url.rstrip("/") + "/api/v1/admin/accounts"
    headers = _build_admin_headers(api_key)
    normalized_name = (account_name or "").strip().lower()

    response = cffi_requests.get(
        url,
        params={
            "page": 1,
            "page_size": 100,
            "platform": "openai",
            "type": "oauth",
            "search": account_name,
        },
        headers=headers,
        proxies=None,
        timeout=20,
        impersonate="chrome110",
    )

    if response.status_code != 200:
        raise RuntimeError(_extract_error_message(response, f"查询 Sub2API 账号失败: HTTP {response.status_code}"))

    data = _extract_response_data(response)
    if not isinstance(data, dict):
        return None

    items = data.get("items")
    if not isinstance(items, list):
        return None

    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("platform", "")).strip().lower() != "openai":
            continue
        if str(item.get("name", "")).strip().lower() == normalized_name:
            return item
    return None


def _sync_account_to_target_groups(
    acc: Account,
    api_url: str,
    api_key: str,
    target_group_ids: List[int],
    concurrency: int,
    priority: int,
    operation_seed: str,
) -> Tuple[bool, str]:
    try:
        existing = _find_existing_sub2api_account(api_url, api_key, acc.email)
    except Exception as exc:
        logger.error("Sub2API 查询账号失败 %s: %s", acc.email, exc)
        return False, str(exc)

    account_hash = hashlib.sha1(acc.email.encode("utf-8")).hexdigest()[:12]
    common_kwargs = {
        "headers": _build_admin_headers(api_key, f"{operation_seed}-{account_hash}"),
        "proxies": None,
        "timeout": 30,
        "impersonate": "chrome110",
    }

    if existing and existing.get("id"):
        url = api_url.rstrip("/") + f"/api/v1/admin/accounts/{existing['id']}"
        payload = _build_grouped_account_update_payload(acc, target_group_ids, concurrency, priority)
        response = cffi_requests.put(url, json=payload, **common_kwargs)
        if response.status_code == 200:
            return True, "updated"
        return False, _extract_error_message(response, f"更新账号失败: HTTP {response.status_code}")

    url = api_url.rstrip("/") + "/api/v1/admin/accounts"
    payload = _build_grouped_account_create_payload(acc, target_group_ids, concurrency, priority)
    response = cffi_requests.post(url, json=payload, **common_kwargs)
    if response.status_code in (200, 201):
        return True, "created"
    return False, _extract_error_message(response, f"创建账号失败: HTTP {response.status_code}")


def _upload_accounts_by_import(
    accounts: List[Account],
    api_url: str,
    api_key: str,
    concurrency: int,
    priority: int,
) -> Tuple[bool, str]:
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    account_items = []

    for acc in accounts:
        if not acc.access_token:
            continue
        account_items.append(_build_import_account_item(acc, concurrency, priority))

    if not account_items:
        return False, "所有账号均缺少 access_token，无法上传"

    payload = {
        "data": {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": exported_at,
            "proxies": [],
            "accounts": account_items,
        },
        "skip_default_group_bind": True,
    }

    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = _build_admin_headers(api_key, f"import-{exported_at}")

    try:
        response = cffi_requests.post(
            url,
            json=payload,
            headers=headers,
            proxies=None,
            timeout=30,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201):
            return True, f"成功导入 {len(account_items)} 个账号"

        return False, _extract_error_message(response, f"上传失败: HTTP {response.status_code}")
    except Exception as exc:
        logger.error("Sub2API 导入异常: %s", exc)
        return False, f"上传异常: {exc}"


def _upload_accounts_with_target_groups(
    accounts: List[Account],
    api_url: str,
    api_key: str,
    target_group_ids: List[int],
    concurrency: int,
    priority: int,
) -> Tuple[bool, str]:
    valid_accounts = [acc for acc in accounts if acc.access_token]
    if not valid_accounts:
        return False, "所有账号均缺少 access_token，无法上传"

    operation_seed = datetime.now(timezone.utc).strftime("sync-%Y%m%d%H%M%S")
    success_count = 0
    failed_messages: List[str] = []

    for acc in valid_accounts:
        success, message = _sync_account_to_target_groups(
            acc,
            api_url,
            api_key,
            target_group_ids,
            concurrency,
            priority,
            operation_seed,
        )
        if success:
            success_count += 1
            continue
        failed_messages.append(f"{acc.email}: {message}")

    if success_count == len(valid_accounts):
        return True, f"成功同步 {success_count} 个账号到分组 {target_group_ids}"
    if success_count > 0:
        return False, f"部分同步成功，成功 {success_count} 个，失败 {len(failed_messages)} 个；首个错误: {failed_messages[0]}"
    return False, failed_messages[0] if failed_messages else "上传失败"


def upload_to_sub2api(
    accounts: List[Account],
    api_url: str,
    api_key: str,
    target_group_ids: Optional[List[int]] = None,
    concurrency: int = 3,
    priority: int = 50,
) -> Tuple[bool, str]:
    """
    上传账号列表到 Sub2API。

    配置了 target_group_ids 时：
    - 先按邮箱名查找账号
    - 已存在则更新账号和分组
    - 不存在则创建账号并绑定分组

    未配置 target_group_ids 时：
    - 沿用旧的导入接口
    """
    if not accounts:
        return False, "无可上传的账号"
    if not api_url:
        return False, "Sub2API URL 未配置"
    if not api_key:
        return False, "Sub2API API Key 未配置"

    normalized_group_ids = [int(group_id) for group_id in (target_group_ids or []) if int(group_id) > 0]
    if normalized_group_ids:
        return _upload_accounts_with_target_groups(
            accounts,
            api_url,
            api_key,
            normalized_group_ids,
            concurrency,
            priority,
        )
    return _upload_accounts_by_import(accounts, api_url, api_key, concurrency, priority)


def batch_upload_to_sub2api(
    account_ids: List[int],
    api_url: str,
    api_key: str,
    target_group_ids: Optional[List[int]] = None,
    concurrency: int = 3,
    priority: int = 50,
) -> dict:
    """
    批量上传指定 ID 的账号到 Sub2API 平台。

    配置了目标分组时返回逐账号结果；
    否则沿用原有导入模式。
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": [],
    }

    normalized_group_ids = [int(group_id) for group_id in (target_group_ids or []) if int(group_id) > 0]

    with get_db() as db:
        accounts: List[Account] = []
        for account_id in account_ids:
            acc = db.query(Account).filter(Account.id == account_id).first()
            if not acc:
                results["failed_count"] += 1
                results["details"].append({"id": account_id, "email": None, "success": False, "error": "账号不存在"})
                continue
            if not acc.access_token:
                results["skipped_count"] += 1
                results["details"].append({"id": account_id, "email": acc.email, "success": False, "error": "缺少 access_token"})
                continue
            accounts.append(acc)

        if not accounts:
            return results

        if normalized_group_ids:
            operation_seed = datetime.now(timezone.utc).strftime("sync-%Y%m%d%H%M%S")
            for acc in accounts:
                success, message = _sync_account_to_target_groups(
                    acc,
                    api_url,
                    api_key,
                    normalized_group_ids,
                    concurrency,
                    priority,
                    operation_seed,
                )
                if success:
                    results["success_count"] += 1
                    results["details"].append({"id": acc.id, "email": acc.email, "success": True, "message": message})
                else:
                    results["failed_count"] += 1
                    results["details"].append({"id": acc.id, "email": acc.email, "success": False, "error": message})
            return results

        success, message = _upload_accounts_by_import(accounts, api_url, api_key, concurrency, priority)
        if success:
            for acc in accounts:
                results["success_count"] += 1
                results["details"].append({"id": acc.id, "email": acc.email, "success": True, "message": message})
        else:
            for acc in accounts:
                results["failed_count"] += 1
                results["details"].append({"id": acc.id, "email": acc.email, "success": False, "error": message})
        return results


def test_sub2api_connection(api_url: str, api_key: str) -> Tuple[bool, str]:
    """
    测试 Sub2API 连接，使用 GET /api/v1/admin/accounts/data 探活。
    """
    if not api_url:
        return False, "API URL 不能为空"
    if not api_key:
        return False, "API Key 不能为空"

    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = {"x-api-key": api_key}

    try:
        response = cffi_requests.get(
            url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201, 204, 405):
            return True, "Sub2API 连接测试成功"
        if response.status_code == 401:
            return False, "连接成功，但 API Key 无效"
        if response.status_code == 403:
            return False, "连接成功，但权限不足"
        return False, f"服务器返回异常状态码: {response.status_code}"

    except cffi_requests.exceptions.ConnectionError as exc:
        return False, f"无法连接到服务器: {exc}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as exc:
        return False, f"连接测试失败: {exc}"
