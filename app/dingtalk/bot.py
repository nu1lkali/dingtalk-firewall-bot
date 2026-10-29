#!/usr/bin/env python3
"""
钉钉机器人运行体。

两种接入方式：
  * stream   —— 官方 Stream 模式长连接，服务端无需公网 IP / 不用配回调地址（推荐）
  * callback —— HTTP 回调（群机器人 outgoing），需公网可达，验签后同步返回结果

无论哪种方式，消息最终都进入 handle_message() 统一处理。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

from app.core import rules_service
from app.dingtalk import parser as cmd_parser
from app.dingtalk import render
from app.settings import settings
from app.store import now_str, store
from app.utils.logger import logger


# --------------------------------------------------------------------------- 指令处理
def _do_allow(parsed: Dict[str, Any], payload: Dict[str, Any]) -> str:
    cfg = settings.section("dingtalk")
    ports = parsed["ports"] or cfg.get("default_ports") or "ALL"
    protocol = parsed["protocol"] or cfg.get("default_protocol") or "tcp"
    ttl = int(cfg.get("default_ttl_minutes") or 1440)
    actor = payload.get("sender_nick") or payload.get("sender_id") or "dingtalk"
    result = rules_service.allow_ip(
        ",".join(parsed["ips"]),
        ports=ports,
        protocol=protocol,
        description=f"钉钉 {actor}",
        source="dingtalk",
        actor=actor,
        ttl_minutes=ttl,
    )
    return render.allow_reply(result, ttl=ttl, actor=actor)


def _do_delete(parsed: Dict[str, Any], payload: Dict[str, Any]) -> Any:
    ips = parsed["ips"]
    ports = parsed["ports"]
    actor = payload.get("sender_nick") or payload.get("sender_id") or "dingtalk"
    targets = []
    for row in store.find_rules(status_in=["active"]):
        if row["ip"] not in ips:
            continue
        if ports and ports != "ALL":
            wanted = {p.replace("-", "/") for p in ports.split(",")}
            if row["port"].replace("-", "/") not in wanted:
                continue
        targets.append(row)

    done = failed = 0
    for row in targets:
        if rules_service.delete_rule(row["id"], actor=actor).get("ok"):
            done += 1
        else:
            failed += 1
    return render.delete_reply(ips, done, failed, targets)


def handle_message(payload: Dict[str, Any]) -> Optional[Any]:
    """
    处理一条机器人消息，返回要回复的内容。

    返回 ``render.PlainText`` 表示走纯文本消息，普通 str 表示 markdown 卡片，
    ``None`` 表示忽略。
    """
    cfg = settings.section("dingtalk")
    text = payload.get("text", "")
    conversation_id = payload.get("conversation_id", "")
    sender_id = payload.get("sender_id", "")
    actor = payload.get("sender_nick") or sender_id or "dingtalk"

    allowed_convs = cfg.get("allowed_conversations") or []
    allowed_users = cfg.get("allowed_users") or []
    if allowed_convs and conversation_id not in allowed_convs:
        reply = render.error_reply(
            "未授权的会话",
            f"会话 {conversation_id or '未知'} 不在白名单中。",
            "请联系管理员在 Web 端「钉钉机器人 → 访问控制」中添加。",
        )
        store.add_bot_message(
            {
                "conversation_id": conversation_id,
                "conversation_title": payload.get("conversation_title", ""),
                "sender_id": sender_id,
                "sender_nick": payload.get("sender_nick", ""),
                "text": text,
                "intent": "denied",
                "reply": "群未授权",
                "status": "denied",
            }
        )
        return reply

    if allowed_users and sender_id not in allowed_users:
        store.add_bot_message(
            {
                "conversation_id": conversation_id,
                "sender_id": sender_id,
                "sender_nick": payload.get("sender_nick", ""),
                "text": text,
                "intent": "denied",
                "reply": "用户未授权",
                "status": "denied",
            }
        )
        return render.error_reply("未授权的操作人", f"userId {sender_id} 不在白名单中。")

    if cfg.get("require_at", True) and payload.get("is_at") is False:
        logger.info(f"忽略未 @机器人 的消息: {text[:60]!r}")
        store.add_bot_message(
            {
                "conversation_id": conversation_id,
                "conversation_title": payload.get("conversation_title", ""),
                "sender_id": sender_id,
                "sender_nick": payload.get("sender_nick", ""),
                "text": text,
                "intent": "ignored",
                "reply": "未 @机器人，已忽略",
                "status": "ignored",
            }
        )
        return None

    parsed = cmd_parser.parse(text)
    intent = parsed["intent"]

    try:
        if intent == "help":
            reply = render.help_card()
        elif intent == "status":
            from app.cron.scheduler import scheduler_service

            reply = render.status_card(
                rules_service.stats(), scheduler_service.status(), stream_runner.status()
            )
        elif intent == "list":
            rules = store.list_rules(status="active", limit=15)
            reply = render.list_card(rules, total=len(store.list_rules(status="active", limit=999)))
        elif intent == "myip":
            client_ip = payload.get("client_ip") or ""
            reply = render.myip_reply(client_ip, actor)
        elif intent == "delete":
            reply = _do_delete(parsed, payload)
        elif intent == "renew":
            reply = _do_renew(parsed, payload)
        elif intent == "allow":
            reply = _do_allow(parsed, payload)
        else:
            reply = render.unknown_reply(text)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"处理机器人指令异常: {exc}", exc_info=True)
        reply = render.error_reply("执行出错", str(exc), "请查看服务端日志定位具体原因。")

    store.add_bot_message(
        {
            "conversation_id": conversation_id,
            "conversation_title": payload.get("conversation_title", ""),
            "sender_id": sender_id,
            "sender_nick": payload.get("sender_nick", ""),
            "text": text,
            "intent": intent,
            "reply": reply,
            "status": "success",
        }
    )
    return reply


# --------------------------------------------------------------------------- Stream 模式
def _build_handler():
    import dingtalk_stream

    class Handler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback):  # noqa: D401
            try:
                data = getattr(callback, "data", {}) or {}
                msg = dingtalk_stream.ChatbotMessage.from_dict(data)
                content = getattr(getattr(msg, "text", None), "content", "") or ""
                payload = {
                    "text": content.strip(),
                    "sender_id": getattr(msg, "sender_staff_id", "") or getattr(msg, "sender_id", ""),
                    "sender_nick": getattr(msg, "sender_nick", "") or "",
                    "conversation_id": getattr(msg, "conversation_id", "") or "",
                    "conversation_title": getattr(msg, "conversation_title", "") or "",
                    "is_at": getattr(msg, "is_in_at_list", True),
                    "client_ip": "",
                }
                stream_runner.state["messages"] = stream_runner.state.get("messages", 0) + 1
                stream_runner.state["last_message_at"] = now_str()
                reply = await asyncio.to_thread(handle_message, payload)
                if reply:
                    # 纯文本回复走 text 消息，避免钉钉把它当 markdown 渲染成卡片
                    if isinstance(reply, render.PlainText):
                        self.reply_text(str(reply), msg)
                    else:
                        self.reply_markdown("IP 放行助手", reply, msg)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Stream 消息处理异常: {exc}", exc_info=True)
                return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, str(exc)

    return Handler()


class DingTalkStreamRunner:
    """管理 Stream 长连接线程。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self.state: Dict[str, Any] = {
            "running": False,        # 线程是否存活
            "connected": False,      # 长连接是否真正建立
            "connected_at": "",
            "endpoint": "",
            "last_error": "",
            "messages": 0,           # 已处理的机器人消息
            "frames": 0,             # 收到的原始帧（含系统帧）
            "last_frame_at": "",
            "last_topic": "",
            "last_message_at": "",
            "topics": [],
        }

    def start(self) -> Dict[str, Any]:
        cfg = settings.section("dingtalk")
        if not cfg.get("enabled"):
            self.state.update({"running": False, "last_error": "钉钉机器人未启用"})
            return self.state
        if cfg.get("mode") != "stream":
            self.state.update({"running": False, "last_error": "当前为 HTTP 回调模式，无需长连接"})
            return self.state
        if self._thread and self._thread.is_alive():
            return self.state
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            self.state.update({"running": False, "last_error": "缺少 Client ID / Client Secret"})
            return self.state

        self._thread = threading.Thread(target=self._run, name="dingtalk-stream", daemon=True)
        self._thread.start()
        logger.info("钉钉 Stream 长连接线程已启动")
        return self.state

    def _run(self) -> None:
        try:
            import dingtalk_stream
        except ImportError:
            self.state.update(
                {"running": False, "last_error": "未安装 dingtalk-stream，请 pip install dingtalk-stream"}
            )
            logger.error("未安装 dingtalk-stream")
            return

        cfg = settings.section("dingtalk")
        try:
            _attach_sdk_logger()
            credential = dingtalk_stream.Credential(
                cfg.get("client_id", ""), cfg.get("client_secret", "")
            )
            client = dingtalk_stream.DingTalkStreamClient(credential)
            topic = dingtalk_stream.ChatbotMessage.TOPIC
            client.register_callback_handler(topic, _build_handler())
            self._client = client
            self.state.update(
                {
                    "running": True,
                    "connected": False,
                    "topics": [topic],
                    "last_error": "",
                }
            )
            self._instrument(client, topic)
            logger.info(f"钉钉 Stream 客户端开始连接，订阅主题 {topic}")

            # SDK 的 start_forever() 是「阻塞 + 自动重连」循环，正好符合守护线程模型
            if hasattr(client, "start_forever"):
                client.start_forever()
            else:  # 兼容旧版 SDK：start() 为协程，执行一次
                asyncio.run(client.start())
        except Exception as exc:  # noqa: BLE001
            logger.error(f"钉钉 Stream 连接异常: {exc}", exc_info=True)
            self.state.update({"running": False, "connected": False, "last_error": str(exc)})
        finally:
            self.state["running"] = False
            self.state["connected"] = False

    def _instrument(self, client, topic: str) -> None:
        """
        给 SDK 客户端加观测点：
          * open_connection 成功才置 connected=True（原来连接还没建立就显示"在线"）
          * route_message 记录每一帧的 topic，便于判断消息是没来、还是来了没路由到
        """
        original_open = client.open_connection

        def wrapped_open():
            try:
                connection = original_open()
            except Exception as exc:  # noqa: BLE001
                self.state.update({"connected": False, "last_error": str(exc)})
                logger.error(f"打开 Stream 长连接异常: {exc}")
                return None
            if connection:
                self.state.update(
                    {
                        "connected": True,
                        "connected_at": now_str(),
                        "endpoint": connection.get("endpoint", ""),
                        "last_error": "",
                    }
                )
                logger.info(f"钉钉 Stream 已连接: {connection.get('endpoint', '')}")
            else:
                self.state.update(
                    {"connected": False, "last_error": "打开长连接失败：凭证错误或被拒绝"}
                )
                logger.error("打开 Stream 长连接失败（open_connection 返回空）")
            return connection

        client.open_connection = wrapped_open

        original_route = client.route_message

        async def wrapped_route(json_message):
            self.state["frames"] = self.state.get("frames", 0) + 1
            self.state["last_frame_at"] = now_str()
            headers = {}
            try:
                headers = json_message.get("headers") or {}
            except AttributeError:
                pass
            frame_topic = headers.get("topic", "") or json_message.get("type", "")
            self.state["last_topic"] = frame_topic
            if frame_topic != "SYSTEM":
                logger.info(f"Stream 收到帧 type={json_message.get('type')} topic={frame_topic}")
                logger.debug(
                    "帧内容: " + json.dumps(json_message, ensure_ascii=False, default=str)[:1500]
                )
            return await original_route(json_message)

        client.route_message = wrapped_route

    def stop(self) -> None:
        client = self._client
        if client is not None:
            for attr in ("websocket", "ws", "_websocket"):
                ws = getattr(client, attr, None)
                close = getattr(ws, "close", None)
                if close:
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            asyncio.run(result)
                    except Exception:  # noqa: BLE001
                        pass
                    break
        self.state["running"] = False
        self.state["connected"] = False
        logger.info("钉钉 Stream 长连接已停止")

    def restart(self) -> Dict[str, Any]:
        self.stop()
        time.sleep(0.5)
        return self.start()

    def status(self) -> Dict[str, Any]:
        alive = bool(self._thread and self._thread.is_alive())
        cfg = settings.section("dingtalk")
        return {
            "enabled": bool(cfg.get("enabled")),
            "mode": cfg.get("mode", "stream"),
            "thread_alive": alive,
            "credential_ready": bool(cfg.get("client_id") and cfg.get("client_secret")),
            **self.state,
        }


def _attach_sdk_logger() -> None:
    """把 dingtalk_stream 自身的日志接进统一日志（含"未知 topic"告警），别让它丢到控制台黑盒里。"""
    ds_logger = logging.getLogger("dingtalk_stream")
    if getattr(ds_logger, "_dcf_attached", False):
        return
    ds_logger.handlers = list(logger.handlers)
    ds_logger.propagate = False
    ds_logger.setLevel(logging.INFO)
    ds_logger._dcf_attached = True  # type: ignore[attr-defined]


stream_runner = DingTalkStreamRunner()


# --------------------------------------------------------------------------- HTTP 回调
def verify_callback_signature(timestamp: str, sign: str, secret: str) -> bool:
    """钉钉 HTTP 回调验签：sign = Base64(HmacSHA256(timestamp + "\\n" + secret, secret))"""
    if not secret:
        return True
    if not timestamp or not sign:
        return False
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, sign)


def handle_callback(data: Dict[str, Any], client_ip: str = "") -> Dict[str, Any]:
    """处理 HTTP 回调请求体，返回钉钉要求格式的响应。"""
    payload = {
        "text": ((data.get("text") or {}).get("content") or "").strip(),
        "sender_id": data.get("senderStaffId") or data.get("senderId", ""),
        "sender_nick": data.get("senderNick", ""),
        "conversation_id": data.get("conversationId", ""),
        "conversation_title": data.get("conversationTitle", ""),
        "is_at": data.get("isInAtList", True),
        "client_ip": client_ip,
    }
    if payload["text"] == "ping":
        return {"msgtype": "empty"}
    reply = handle_message(payload)
    if not reply:
        return {"msgtype": "empty"}
    if isinstance(reply, render.PlainText):
        return {"msgtype": "text", "text": {"content": str(reply)}}
    return {
        "msgtype": "markdown",
        "markdown": {"title": "IP 放行助手", "text": reply},
    }
