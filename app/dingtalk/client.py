#!/usr/bin/env python3
"""
钉钉开放平台 API 封装（新版 v1.0 + 兼容旧版 oapi）。

涉及接口：
    获取凭证        POST https://api.dingtalk.com/v1.0/oauth2/accessToken
    群消息发送      POST https://api.dingtalk.com/v1.0/robot/groupMessages/send
    单聊消息发送    POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend
    会话回执        POST <sessionWebhook>            （机器人被动回复，最稳定）
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from app.utils.logger import logger

API_BASE = "https://api.dingtalk.com"
OAPI_BASE = "https://oapi.dingtalk.com"


class DingTalkError(RuntimeError):
    pass


class DingTalkAPI:
    """轻量级同步客户端，token 带缓存。"""

    def __init__(self, client_id: str, client_secret: str, robot_code: str = ""):
        if not client_id or not client_secret:
            raise DingTalkError("钉钉 Client ID / Client Secret 未配置")
        self.client_id = client_id
        self.client_secret = client_secret
        self.robot_code = robot_code or client_id
        self._token = ""
        self._expire_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ token
    def get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expire_at - 120:
                return self._token
            try:
                resp = requests.post(
                    f"{API_BASE}/v1.0/oauth2/accessToken",
                    json={"appKey": self.client_id, "appSecret": self.client_secret},
                    timeout=10,
                )
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise DingTalkError(f"获取 access_token 失败: {exc}") from exc
            if resp.status_code != 200 or not data.get("accessToken"):
                raise DingTalkError(
                    f"获取 access_token 失败: HTTP {resp.status_code} {data}"
                )
            self._token = data["accessToken"]
            self._expire_at = time.time() + int(data.get("expireIn", 7200))
            return self._token

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = self.get_token()
        try:
            resp = requests.post(
                f"{API_BASE}{path}",
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise DingTalkError(f"请求钉钉接口失败: {exc}") from exc
        if resp.status_code >= 400:
            raise DingTalkError(f"钉钉接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json() if resp.text else {}
        except ValueError:
            return {}

    # ------------------------------------------------------------------ 发送
    @staticmethod
    def _markdown_param(title: str, text: str) -> str:
        return json.dumps({"title": title, "text": text}, ensure_ascii=False)

    def send_group_markdown(
        self, open_conversation_id: str, title: str, text: str
    ) -> Dict[str, Any]:
        if not open_conversation_id:
            raise DingTalkError("缺少 openConversationId")
        return self._post(
            "/v1.0/robot/groupMessages/send",
            {
                "robotCode": self.robot_code,
                "openConversationId": open_conversation_id,
                "msgKey": "sampleMarkdown",
                "msgParam": self._markdown_param(title, text),
            },
        )

    def send_group_text(self, open_conversation_id: str, text: str) -> Dict[str, Any]:
        return self._post(
            "/v1.0/robot/groupMessages/send",
            {
                "robotCode": self.robot_code,
                "openConversationId": open_conversation_id,
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            },
        )

    def send_o2o_markdown(self, user_ids: List[str], title: str, text: str) -> Dict[str, Any]:
        if not user_ids:
            raise DingTalkError("缺少接收人 userId")
        return self._post(
            "/v1.0/robot/oToMessages/batchSend",
            {
                "robotCode": self.robot_code,
                "userIds": user_ids,
                "msgKey": "sampleMarkdown",
                "msgParam": self._markdown_param(title, text),
            },
        )

    def reply_session_webhook(
        self, webhook: str, title: str, text: str, at_user_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """机器人被动回复：向消息自带的 sessionWebhook 回发，无需额外权限。"""
        if not webhook:
            raise DingTalkError("消息缺少 sessionWebhook，无法回执")
        body: Dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        }
        if at_user_ids:
            body["at"] = {"atUserIds": at_user_ids, "isAtAll": False}
        try:
            resp = requests.post(webhook, json=body, timeout=10)
        except requests.RequestException as exc:
            raise DingTalkError(f"回执失败: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if data.get("errcode") not in (0, None):
            raise DingTalkError(f"回执失败: {data}")
        return data

    # ------------------------------------------------------------------ 校验
    def ping(self) -> Dict[str, Any]:
        token = self.get_token(force=True)
        return {"ok": bool(token), "token_prefix": token[:6] + "***"}


def build_api() -> DingTalkAPI:
    from app.settings import settings

    cfg = settings.section("dingtalk")
    return DingTalkAPI(
        client_id=cfg.get("client_id") or cfg.get("app_id", ""),
        client_secret=cfg.get("client_secret", ""),
        robot_code=cfg.get("robot_code") or cfg.get("client_id", ""),
    )


def notify(text: str, title: str = "动态云防火墙通知") -> bool:
    """向指定会话推送通知（webhook / 定时任务使用），失败仅记日志。"""
    from app.settings import settings

    cfg = settings.section("dingtalk")
    if not cfg.get("enabled"):
        return False
    conversation = cfg.get("notify_conversation_id") or ""
    if not conversation:
        return False
    try:
        build_api().send_group_markdown(conversation, title, text)
        return True
    except DingTalkError as exc:
        logger.warning(f"钉钉通知发送失败: {exc}")
        return False
