"""NapCat HTTP 客户端封装。

负责与 NapCat 的 HTTP API 通信，包括：
- 接收群消息（HTTP 上报）
- 调用各种 OneBot API 发送消息
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Callable, Optional
import requests

from .utils.logger import get_logger

logger = get_logger("napcat_client")


class NapCatClient:
    """NapCat HTTP API 客户端。"""

    def __init__(self, base_url: str, group_id: str):
        self.base_url = base_url.rstrip("/")
        self.group_id = group_id
        self.self_info: dict = {}          # {user_id, nickname}
        self.member_cache: dict = {}        # {qq: {nickname, card, role, title}}

    # ---------- 启动预热 ----------
    def warmup(self):
        """启动时预热：拉取自身信息和群成员列表。"""
        self.self_info = self.get_login_info()
        logger.info(f"机器人身份：{self.self_info.get('nickname')}({self.self_info.get('user_id')})")
        members = self.get_group_member_list()
        for m in members:
            qq = str(m.get("user_id"))
            detail = self.get_group_member_info(qq)
            self.member_cache[qq] = {
                "nickname": detail.get("nickname", ""),
                "card": detail.get("card", ""),
                "role": detail.get("role", "member"),
                "title": detail.get("title", ""),
            }
        logger.info(f"群成员缓存：{len(self.member_cache)} 人")

    def get_nickname(self, qq: str) -> str:
        """从缓存取昵称（优先群名片）。"""
        info = self.member_cache.get(qq, {})
        return info.get("card") or info.get("nickname") or qq

    # ---------- OneBot API 调用 ----------
    def _call(self, endpoint: str, payload: dict, quiet: bool = False) -> dict:
        """通用 API 调用。

        检查业务 status：NapCat 返回 {"status":"ok"/"failed", "retcode":..., "message":...}。
        status != "ok" 时记日志并返回空 dict，让调用方能感知失败（调用方对返回值
        做 .get("data", ...) 时会拿到默认值，不会误认为成功）。

        Args:
            quiet: True 时业务失败记 INFO（用于重试场景，避免重试期间的正常失败
                   刷 ERROR）；False（默认）记 ERROR，保持其他调用方的既有行为。
        """
        url = f"{self.base_url}/{endpoint}"
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # 检查业务状态（NapCat 失败时 HTTP 仍是 200）
            if isinstance(data, dict) and data.get("status") != "ok":
                retcode = data.get("retcode", "?")
                message = data.get("message", "") or data.get("wording", "")
                log_msg = f"调用 {endpoint} 业务失败: retcode={retcode} message={message} payload={payload}"
                if quiet:
                    logger.info(log_msg)
                else:
                    logger.error(log_msg)
                return {}
            return data
        except Exception as e:
            logger.error(f"调用 {endpoint} 失败: {e}")
            return {}

    def get_login_info(self) -> dict:
        return self._call("get_login_info", {}).get("data", {})

    def get_group_member_list(self) -> list:
        return self._call("get_group_member_list", {"group_id": self.group_id}).get("data", [])

    def get_group_member_info(self, user_id: str) -> dict:
        return self._call("get_group_member_info", {
            "group_id": self.group_id,
            "user_id": user_id,
        }).get("data", {})

    def send_group_msg(self, message: list) -> dict:
        """发送群消息（消息段数组形式）。"""
        return self._call("send_group_msg", {
            "group_id": self.group_id,
            "message": message,
        })

    def send_group_ai_record(self, character: str, text: str) -> dict:
        """发送 AI 语音。"""
        return self._call("send_group_ai_record", {
            "group_id": self.group_id,
            "character": character,
            "text": text,
        })

    def get_ai_characters(self) -> list:
        """获取群聊可用的 AI 语音角色列表（启动探测用）。

        返回 [{"character_id": "...", "character_name": "...", "preview_url": "..."}, ...]
        失败返回空列表。
        """
        resp = self._call("get_ai_characters", {
            "group_id": self.group_id,
            "chat_type": 1,  # 群聊
        })
        if not resp:
            return []
        data = resp.get("data", [])
        # data 是 [{"type":..., "characters":[...]}] 结构，展平取 characters
        characters = []
        for item in data if isinstance(data, list) else []:
            characters.extend(item.get("characters", []))
        return characters

    def send_group_forward_msg(self, messages: list, title: str = "") -> dict:
        """发送合并转发。"""
        return self._call("send_group_forward_msg", {
            "group_id": self.group_id,
            "messages": messages,
            "title": title,
        })

    def set_msg_emoji_like(self, message_id: str, emoji_id: str) -> dict:
        """对消息做 emoji 反应。"""
        return self._call("set_msg_emoji_like", {
            "message_id": message_id,
            "emoji_id": emoji_id,
        })

    def fetch_ptt_text(self, message_id: str, quiet: bool = False) -> str:
        """获取语音转文字结果（/fetch_ptt_text）。

        Args:
            message_id: 语音消息的 message_id
            quiet: True 时业务失败记 INFO（语音转写重试场景），否则记 ERROR。

        Returns:
            转写的文字；失败（含权限不足、非语音消息、NapCat 不支持）返回空串。
        """
        resp = self._call("fetch_ptt_text", {"message_id": message_id}, quiet=quiet)
        if not resp:
            return ""
        data = resp.get("data") or {}
        return data.get("text", "") or ""


class NapCatWebhookServer:
    """接收 NapCat HTTP 上报的 webhook 服务。

    NapCat 收到群消息后会 POST 到本服务，触发回调。
    若设置 target_group_id，则只处理该群消息，其他群消息直接丢弃。
    群消息走 on_message 回调，撤回通知（group_recall）走 on_recall 回调。
    """

    def __init__(self, host: str, port: int, on_message: Callable[[dict], None],
                 target_group_id: Optional[str] = None,
                 on_recall: Optional[Callable[[dict], None]] = None):
        self.host = host
        self.port = port
        self.on_message = on_message
        self.target_group_id = target_group_id
        self.on_recall = on_recall
        self._server: Optional[HTTPServer] = None

    def start(self):
        """启动 webhook 服务（阻塞）。"""
        on_message = self.on_message
        on_recall = self.on_recall
        target_group_id = self.target_group_id

        class Handler(BaseHTTPRequestHandler):
            def _read_body(self):
                """读取请求 body，支持 Content-Length 和 chunked transfer encoding。"""
                content_length = self.headers.get("Content-Length")
                if content_length:
                    return self.rfile.read(int(content_length))
                # chunked transfer encoding
                if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                    body = b""
                    while True:
                        line = self.rfile.readline()
                        if not line:
                            break
                        chunk_size = int(line.strip(), 16)
                        if chunk_size == 0:
                            self.rfile.readline()  # 读取最后的 \r\n
                            break
                        body += self.rfile.read(chunk_size)
                        self.rfile.readline()  # 读取 chunk 后的 \r\n
                    return body
                return b""

            def do_POST(self):
                body = self._read_body()
                # 调试：把原始数据写到独立文件
                try:
                    with open("debug_webhook.log", "ab") as f:
                        f.write(b"=== POST ===\n")
                        f.write(f"length={len(body)}\n".encode("utf-8"))
                        f.write(f"headers={dict(self.headers)}\n".encode("utf-8"))
                        f.write(b"body=")
                        f.write(body)
                        f.write(b"\n\n")
                except Exception:
                    pass
                logger.info(f"收到 POST 上报，长度={len(body)}")
                try:
                    data = json.loads(body)
                    post_type = data.get("post_type", "")
                    logger.info(f"解析成功，post_type={post_type}")
                    if post_type == "message" and data.get("message_type") == "group":
                        # 群过滤：只处理目标群消息，其他群直接丢弃
                        msg_group_id = str(data.get("group_id", ""))
                        if target_group_id and msg_group_id != target_group_id:
                            logger.debug(
                                f"丢弃非目标群消息：group_id={msg_group_id} "
                                f"(期望 {target_group_id}) user={data.get('user_id')}"
                            )
                        else:
                            # 异步处理消息，do_POST 立即返回 200，避免阻塞 NapCat
                            Thread(target=on_message, args=(data,), daemon=True).start()
                    elif post_type == "notice" and data.get("notice_type") == "group_recall":
                        # 群撤回通知：群过滤后异步调用 on_recall
                        msg_group_id = str(data.get("group_id", ""))
                        if target_group_id and msg_group_id != target_group_id:
                            logger.debug(
                                f"丢弃非目标群撤回通知：group_id={msg_group_id} "
                                f"(期望 {target_group_id})"
                            )
                        elif on_recall:
                            Thread(target=on_recall, args=(data,), daemon=True).start()
                except Exception as e:
                    logger.error(f"处理上报失败: {e}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_GET(self):
                # 支持 GET 健康检查
                try:
                    with open("debug_webhook.log", "ab") as f:
                        f.write(b"=== GET ===\n")
                        f.write(f"path={self.path}\n".encode("utf-8"))
                        f.write(f"headers={dict(self.headers)}\n\n".encode("utf-8"))
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, format, *args):
                pass  # 静默默认日志

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        logger.info(f"webhook 服务监听 {self.host}:{self.port}（ThreadingHTTPServer）")
        self._server.serve_forever()

    def start_async(self):
        """非阻塞启动（用于测试）。"""
        t = Thread(target=self.start, daemon=True)
        t.start()
