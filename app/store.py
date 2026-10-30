#!/usr/bin/env python3
"""
SQLite 数据存储层。

单文件数据库，无需外部服务；使用 WAL 模式配合一把写锁，适配
Flask 多线程 + APScheduler 后台线程的并发写场景。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("APP_DB_PATH") or os.path.join(DATA_DIR, "app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL DEFAULT 'aliyun_swas',
    instance_id     TEXT NOT NULL,
    instance_name   TEXT DEFAULT '',
    rule_id         TEXT DEFAULT '',
    ip              TEXT NOT NULL,
    cidr            TEXT DEFAULT '',
    protocol        TEXT NOT NULL DEFAULT 'tcp',
    port            TEXT NOT NULL DEFAULT 'ALL',
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    source          TEXT NOT NULL DEFAULT 'web',
    actor           TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    expires_at      TEXT DEFAULT '',
    snapshot        TEXT DEFAULT '',
    tag             TEXT DEFAULT '',
    notified_at     TEXT DEFAULT '',
    auto_renew      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rules_instance ON rules(instance_id);
CREATE INDEX IF NOT EXISTS idx_rules_ip ON rules(ip);
CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status);
-- 注意：tag 索引必须等 _migrate() 补列之后再建，否则老库会 no such column: tag

CREATE TABLE IF NOT EXISTS ip_geo (
    ip          TEXT PRIMARY KEY,
    country     TEXT DEFAULT '',
    region      TEXT DEFAULT '',
    city        TEXT DEFAULT '',
    isp         TEXT DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'INFO',
    action      TEXT NOT NULL,
    target      TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'success',
    message     TEXT DEFAULT '',
    detail      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);

CREATE TABLE IF NOT EXISTS bot_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    conversation_id     TEXT DEFAULT '',
    conversation_title  TEXT DEFAULT '',
    sender_id           TEXT DEFAULT '',
    sender_nick         TEXT DEFAULT '',
    text                TEXT DEFAULT '',
    intent              TEXT DEFAULT '',
    reply               TEXT DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'success'
);
CREATE INDEX IF NOT EXISTS idx_bot_ts ON bot_messages(ts DESC);

CREATE TABLE IF NOT EXISTS task_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    job_name    TEXT DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    message     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_started ON task_runs(started_at DESC);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """线程安全的极简数据库封装。"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    # ---------------------------------------------------------------- 迁移
    # 老库是 CREATE TABLE IF NOT EXISTS 建的，新增列不会自动出现，必须手工 ALTER
    _RULE_COLUMNS = {
        "tag": "TEXT DEFAULT ''",
        "notified_at": "TEXT DEFAULT ''",
        "auto_renew": "INTEGER NOT NULL DEFAULT 0",
        "renew_mode": "TEXT DEFAULT 'follow'",
    }

    def _migrate(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(rules)")}
        for name, ddl in self._RULE_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE rules ADD COLUMN {name} {ddl}")
        # 依赖新增列的索引，必须放在补列之后
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_tag ON rules(tag)")

    # ---------------------------------------------------------------- 基础
    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    @staticmethod
    def _row2dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    # ---------------------------------------------------------------- 设置
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now_str()),
        )

    def all_settings(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for row in self._query("SELECT key, value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = None
        return out

    # ---------------------------------------------------------------- 规则
    def add_rule(self, data: Dict[str, Any]) -> int:
        ts = now_str()
        return self._execute(
            """INSERT INTO rules(provider, instance_id, instance_name, rule_id, ip, cidr,
                                 protocol, port, description, status, source, actor,
                                 created_at, updated_at, expires_at, snapshot, tag, notified_at,
                                 auto_renew, renew_mode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("provider", "aliyun_swas"),
                data.get("instance_id", ""),
                data.get("instance_name", ""),
                data.get("rule_id", ""),
                data.get("ip", ""),
                data.get("cidr", ""),
                data.get("protocol", "tcp"),
                data.get("port", "ALL"),
                data.get("description", ""),
                data.get("status", "active"),
                data.get("source", "web"),
                data.get("actor", ""),
                data.get("created_at") or ts,
                data.get("updated_at") or ts,
                data.get("expires_at", ""),
                data.get("snapshot", ""),
                data.get("tag", ""),
                data.get("notified_at", ""),
                1 if data.get("auto_renew") else 0,
                data.get("renew_mode") or "follow",
            ),
        )

    def update_rule(self, rule_pk: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now_str()
        keys = ", ".join(f"{k}=?" for k in fields)
        self._execute(
            f"UPDATE rules SET {keys} WHERE id=?", tuple(fields.values()) + (rule_pk,)
        )

    def delete_rule_row(self, rule_pk: int) -> None:
        self._execute("DELETE FROM rules WHERE id=?", (rule_pk,))

    def get_rule(self, rule_pk: int) -> Optional[Dict[str, Any]]:
        return self._row2dict(
            self._query_one("SELECT * FROM rules WHERE id=?", (rule_pk,))
        )

    def find_rules(self, **filters) -> List[Dict[str, Any]]:
        where, params = self._rules_where(**filters)
        sql = "SELECT * FROM rules"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY id DESC"
        return [dict(r) for r in self._query(sql, tuple(params))]

    @staticmethod
    def _rules_where(
        status: Optional[str] = None,
        keyword: str = "",
        instance_id: Optional[str] = None,
        status_in: Optional[List[str]] = None,
        tag: Optional[str] = None,
        protocol: Optional[str] = None,
        source: Optional[str] = None,
        auto_renew: Optional[bool] = None,
        ip: Optional[str] = None,
        cidr: Optional[str] = None,
        port: Optional[str] = None,
        rule_id: Optional[str] = None,
        **ignored,
    ) -> tuple:
        where, params = [], []
        if status_in:
            where.append("status IN (" + ",".join("?" * len(status_in)) + ")")
            params.extend(status_in)
        elif status and status != "all":
            where.append("status=?")
            params.append(status)
        if instance_id:
            where.append("instance_id=?")
            params.append(instance_id)
        # ip / cidr / port / rule_id 必须真正参与过滤：
        # 早期把它们留空会让「同 IP+端口是否已存在」的判断退化成「该实例最新一条」，
        # 进而把 rule_id、到期时间、自动续期写到别的规则行上（见 _upsert_local）。
        if ip:
            where.append("ip=?")
            params.append(ip)
        if cidr:
            where.append("cidr=?")
            params.append(cidr)
        if port:
            where.append("port=?")
            params.append(port)
        if rule_id:
            where.append("rule_id=?")
            params.append(rule_id)
        if tag:
            where.append("tag=?")
            params.append(tag)
        if protocol:
            where.append("protocol=?")
            params.append(protocol.lower())
        if source:
            where.append("source=?")
            params.append(source)
        if auto_renew is not None:
            where.append("auto_renew=?")
            params.append(1 if auto_renew else 0)
        if keyword:
            where.append("(ip LIKE ? OR description LIKE ? OR port LIKE ? OR tag LIKE ? OR actor LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like] * 5)
        return " AND ".join(where), params

    def list_rules(
        self,
        status: Optional[str] = None,
        keyword: str = "",
        instance_id: Optional[str] = None,
        limit: int = 500,
        status_in: Optional[List[str]] = None,
        tag: Optional[str] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where, params = self._rules_where(
            status=status, keyword=keyword, instance_id=instance_id, status_in=status_in, tag=tag
        )
        sql = "SELECT * FROM rules"
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, max(0, offset)])
        return [dict(r) for r in self._query(sql, tuple(params))]

    def count_rules(
        self,
        status: Optional[str] = None,
        keyword: str = "",
        instance_id: Optional[str] = None,
        status_in: Optional[List[str]] = None,
        tag: Optional[str] = None,
    ) -> int:
        where, params = self._rules_where(
            status=status, keyword=keyword, instance_id=instance_id, status_in=status_in, tag=tag
        )
        sql = "SELECT COUNT(*) AS n FROM rules"
        if where:
            sql += " WHERE " + where
        row = self._query_one(sql, tuple(params))
        return int(row["n"]) if row else 0

    def list_tags(self) -> List[Dict[str, Any]]:
        """标签清单 + 各标签下的「在用」规则数，供前端筛选器使用。"""
        rows = self._query(
            """SELECT tag, COUNT(*) AS total
               FROM rules
               WHERE tag <> '' AND status IN ('active','disabled')
               GROUP BY tag
               ORDER BY total DESC, tag ASC"""
        )
        return [dict(r) for r in rows]

    def expiring_soon_rules(self, minutes: int = 10, now: Optional[str] = None) -> List[Dict[str, Any]]:
        """即将到期且还没提醒过的生效规则。"""
        from datetime import datetime, timedelta

        now = now or now_str()
        horizon = (datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        rows = self._query(
            """SELECT * FROM rules
               WHERE status='active' AND expires_at<>'' AND notified_at=''
                 AND expires_at > ? AND expires_at <= ?
               ORDER BY expires_at ASC""",
            (now, horizon),
        )
        return [dict(r) for r in rows]

    def expired_active_rules(self, now: Optional[str] = None) -> List[Dict[str, Any]]:
        now = now or now_str()
        rows = self._query(
            "SELECT * FROM rules WHERE status='active' AND expires_at<>'' AND expires_at<=?",
            (now,),
        )
        return [dict(r) for r in rows]

    @staticmethod
    def canonical_port(port: str) -> str:
        """端口归一：云端可能返回 "ALL"，本地统一存 "1/65535"，否则匹配不上会重复插入。"""
        text = str(port or "").strip()
        if not text or text.upper() in {"ALL", "*"}:
            return "1/65535"
        return text

    @classmethod
    def natural_key(cls, ip: str, protocol: str, port: str) -> str:
        """规则的业务唯一键：IP + 协议 + 端口（同一实例内一条防火墙规则就是这个组合）。"""
        return "|".join(
            [
                str(ip or "").strip(),
                (protocol or "tcp").strip().lower(),
                cls.canonical_port(port),
            ]
        )

    def replace_instance_rules(self, instance_id: str, cloud_rules: List[Dict[str, Any]]) -> int:
        """
        用云端真实规则刷新本地镜像（保留本地业务元数据：标签 / 到期 / 来源）。

        去重策略（缺一不可，否则每跑一次同步就多一批重复行）：

        1. 先合并历史遗留的同键重复行（否则 rule_id 索引会被重复行污染）；
        2. 按 rule_id 精确匹配云端规则与本地行；
        3. rule_id 对不上（云端 id 变了、或本地行的 rule_id 曾被别的规则覆盖）时，
           退化成「IP + 协议 + 端口」业务键匹配，命中后把该行收编成这条云端规则；
        4. 一条本地行只能被一条云端规则认领，避免互相抢。

        返回本次合并掉的重复行数。
        """
        merged = self.dedupe_duplicate_rules(instance_id=instance_id)
        local = self.find_rules(instance_id=instance_id, status_in=["active", "disabled"])
        by_rule_id: Dict[str, Dict[str, Any]] = {}
        by_natural: Dict[str, List[Dict[str, Any]]] = {}
        for row in sorted(local, key=lambda r: r["id"]):
            if row["rule_id"]:
                by_rule_id.setdefault(row["rule_id"], row)
            by_natural.setdefault(
                self.natural_key(row["ip"], row["protocol"], row["port"]), []
            ).append(row)

        matched: set = set()
        for cr in cloud_rules:
            rule_id = cr.get("rule_id", "") or ""
            nat = self.natural_key(cr.get("ip", ""), cr.get("protocol", ""), cr.get("port", ""))

            hit = by_rule_id.get(rule_id) if rule_id else None
            if hit is not None and hit["id"] in matched:
                hit = None
            if hit is None:
                for cand in by_natural.get(nat, []):
                    if cand["id"] not in matched:
                        hit = cand
                        break

            fields = {
                "ip": cr.get("ip", ""),
                "cidr": cr.get("cidr", ""),
                "protocol": cr.get("protocol", "tcp"),
                "port": self.canonical_port(cr.get("port", "ALL")),
                "description": cr.get("description", ""),
            }
            if hit is not None:
                matched.add(hit["id"])
                fields["rule_id"] = rule_id or hit["rule_id"]
                # 已禁用（本地标记）的规则不因同步而恢复为生效
                fields["status"] = "disabled" if hit["status"] == "disabled" else "active"
                self.update_rule(hit["id"], **fields)
            else:
                self.add_rule(
                    {
                        "instance_id": instance_id,
                        "instance_name": cr.get("instance_name", ""),
                        "rule_id": rule_id,
                        "ip": cr.get("ip", ""),
                        "cidr": cr.get("cidr", ""),
                        "protocol": cr.get("protocol", "tcp"),
                        "port": self.canonical_port(cr.get("port", "ALL")),
                        "description": cr.get("description", ""),
                        "status": "active",
                        "source": "cloud-sync",
                        "actor": "sync",
                    }
                )

        # 云端已不存在的规则标记为 deleted（可能是手动从控制台删掉）
        cloud_ids = {cr.get("rule_id", "") for cr in cloud_rules if cr.get("rule_id")}
        cloud_natural = {
            self.natural_key(cr.get("ip", ""), cr.get("protocol", ""), cr.get("port", ""))
            for cr in cloud_rules
        }
        for row in local:
            if row["status"] != "active":
                continue
            rid = row["rule_id"] or ""
            if (rid and rid in cloud_ids) or self.natural_key(
                row["ip"], row["protocol"], row["port"]
            ) in cloud_natural:
                continue
            self.update_rule(
                row["id"],
                status="deleted",
                description=((row["description"] or "") + " [云端已不存在]")[:120],
            )

        return merged

    def dedupe_duplicate_rules(self, instance_id: Optional[str] = None) -> int:
        """
        合并「同实例 + 同 IP + 同协议 + 同端口」的重复行，返回被合并掉的行数。

        保留哪一条：优先有标签的、其次有到期时间的、最后取 id 最小的（最老的那条），
        其余行合并元数据后标记为 deleted（不物理删除，保留审计痕迹）。
        """
        if instance_id:
            rows = self.find_rules(instance_id=instance_id, status_in=["active", "disabled"])
        else:
            rows = self.find_rules(status_in=["active", "disabled"])

        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            key = (
                str(row["instance_id"] or "")
                + "|"
                + self.natural_key(row["ip"], row["protocol"], row["port"])
            )
            groups.setdefault(key, []).append(row)

        removed = 0
        for items in groups.values():
            if len(items) < 2:
                continue
            keeper = max(
                items,
                key=lambda r: (
                    1 if (r.get("tag") or "") else 0,
                    len(r.get("expires_at") or ""),
                    -int(r["id"]),
                ),
            )
            tags = [r["tag"] for r in items if r.get("tag")]
            expiries = [r["expires_at"] for r in items if r.get("expires_at")]
            rule_ids = [r["rule_id"] for r in items if r.get("rule_id")]
            self.update_rule(
                keeper["id"],
                tag=tags[0] if tags else (keeper.get("tag") or ""),
                expires_at=max(expiries) if expiries else (keeper.get("expires_at") or ""),
                auto_renew=1 if any(r.get("auto_renew") for r in items) else 0,
                rule_id=keeper["rule_id"] or (rule_ids[0] if rule_ids else ""),
                notified_at="",
            )
            for row in items:
                if row["id"] == keeper["id"]:
                    continue
                self.update_rule(
                    row["id"],
                    status="deleted",
                    description=((row["description"] or "") + " [重复行已合并]")[:120],
                )
                removed += 1
        return removed

    # ---------------------------------------------------------------- IP 归属地
    def get_geo(self, ip: str) -> Optional[Dict[str, Any]]:
        return self._row2dict(self._query_one("SELECT * FROM ip_geo WHERE ip=?", (ip,)))

    def get_geo_many(self, ips: List[str]) -> Dict[str, Dict[str, Any]]:
        if not ips:
            return {}
        marks = ",".join("?" * len(ips))
        rows = self._query(f"SELECT * FROM ip_geo WHERE ip IN ({marks})", tuple(ips))
        return {row["ip"]: dict(row) for row in rows}

    def set_geo(self, ip: str, info: Dict[str, Any]) -> None:
        self._execute(
            """INSERT INTO ip_geo(ip, country, region, city, isp, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(ip) DO UPDATE SET
                 country=excluded.country, region=excluded.region,
                 city=excluded.city, isp=excluded.isp, updated_at=excluded.updated_at""",
            (
                ip,
                info.get("country", ""),
                info.get("region", ""),
                info.get("city", ""),
                info.get("isp", ""),
                now_str(),
            ),
        )

    # ---------------------------------------------------------------- 日志
    def log(
        self,
        action: str,
        message: str = "",
        status: str = "success",
        level: str = "INFO",
        target: str = "",
        detail: Any = None,
    ) -> None:
        try:
            detail_str = ""
            if detail is not None:
                detail_str = detail if isinstance(detail, str) else json.dumps(
                    detail, ensure_ascii=False, default=str
                )
            self._execute(
                "INSERT INTO logs(ts, level, action, target, status, message, detail) VALUES(?,?,?,?,?,?,?)",
                (now_str(), level, action, target, status, message, detail_str[:8000]),
            )
        except Exception:  # 日志失败不应影响主流程
            pass

    def list_logs(self, limit: int = 200, keyword: str = "", level: str = "") -> List[Dict[str, Any]]:
        where, params = [], []
        if keyword:
            where.append("(action LIKE ? OR message LIKE ? OR target LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        if level:
            where.append("level=?")
            params.append(level)
        sql = "SELECT * FROM logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._query(sql, tuple(params))]

    def prune_logs(self, keep_days: int = 30) -> int:
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
            cur2 = self._conn.execute("DELETE FROM task_runs WHERE started_at < ?", (cutoff,))
            # 机器人消息同样会无限增长，一起按保留天数清理
            cur3 = self._conn.execute("DELETE FROM bot_messages WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount + cur2.rowcount + cur3.rowcount

    # ---------------------------------------------------------------- 机器人消息
    def add_bot_message(self, data: Dict[str, Any]) -> int:
        return self._execute(
            """INSERT INTO bot_messages(ts, conversation_id, conversation_title, sender_id,
                                        sender_nick, text, intent, reply, status)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                data.get("ts") or now_str(),
                data.get("conversation_id", ""),
                data.get("conversation_title", ""),
                data.get("sender_id", ""),
                data.get("sender_nick", ""),
                data.get("text", ""),
                data.get("intent", ""),
                data.get("reply", ""),
                data.get("status", "success"),
            ),
        )

    def list_bot_messages(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM bot_messages ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def list_conversations(self, limit: int = 20) -> List[Dict[str, Any]]:
        """从指令记录里提取出现过的会话，用于主动推送时选择目标群。"""
        rows = self._query(
            """SELECT conversation_id,
                      MAX(conversation_title) AS conversation_title,
                      COUNT(*)                AS messages,
                      MAX(ts)                 AS last_ts
               FROM bot_messages
               WHERE conversation_id <> ''
               GROUP BY conversation_id
               ORDER BY last_ts DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 任务运行
    def start_task_run(self, job_id: str, job_name: str) -> int:
        return self._execute(
            "INSERT INTO task_runs(job_id, job_name, started_at, status) VALUES(?,?,?,?)",
            (job_id, job_name, now_str(), "running"),
        )

    def finish_task_run(self, run_id: int, status: str, message: str, duration_ms: int) -> None:
        self._execute(
            "UPDATE task_runs SET finished_at=?, status=?, message=?, duration_ms=? WHERE id=?",
            (now_str(), status, (message or "")[:2000], duration_ms, run_id),
        )

    def list_task_runs(self, limit: int = 50, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if job_id:
            rows = self._query(
                "SELECT * FROM task_runs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM task_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 备份
    def backup_to(self, path: str) -> str:
        """
        用 SQLite 官方 backup API 做在线备份（不锁写、不用停机），
        比直接 copy 文件安全——WAL 模式下裸拷会丢 -wal 里未 checkpoint 的数据。
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(path)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        return path


store = Store()
