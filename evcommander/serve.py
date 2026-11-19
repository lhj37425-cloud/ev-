#!/usr/bin/env python3
"""Serve the EV指挥官 preview on localhost."""

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as JobTimeout
from contextlib import closing
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
from threading import BoundedSemaphore
import time
from urllib.parse import parse_qs, quote, urlsplit

import acquisition
import commander
import customer_import
import customers
import digital_human
import dispatch
import geo
import image_generation as images
import image_studio
import inspirations
from invitation import COOKIE, Invitation, login_page
import knowledge
import materials
import memory
import operations
import payments
import studio
from preview import BOOTSTRAP, BRAND_NAME, reply
from manual_publishing import prepare_manual_publishing


ROOT = Path(__file__).resolve().parent / "mirror"
HOME_ROUTE = "/mb/home"
STATIC_FILES = {"/logo.png", "/favicon.png"}
STATIC_DIRS = ("/assets/", "/fonts/")
UPLOAD_SLOTS = BoundedSemaphore(2)
INSPIRATION_DETAIL = re.compile(re.escape(inspirations.PREFIX) + r"([0-9]{1,16})")
CHAT_READ_PATHS = {"/api/chat/conversations", "/api/chat/conversation"}
CHAT_WRITE_PATHS = {
    "/api/chat/send", commander.COMMAND_STREAM, "/api/chat/skill/start",
    "/api/chat/persist-skill", "/api/chat/conversation/delete",
    studio.GUIDE_PATH, studio.STREAM_PATH, dispatch.PLAN_STEP, inspirations.CREATE,
    *images.STREAM_PATHS, *images.EDIT_STREAM_PATHS,
    *images.PRODUCT_BG_STREAM_PATHS, *images.TRYON_STREAM_PATHS,
    *payments.WRITE_PATHS, *digital_human.WRITE_PATHS, *acquisition.WRITE_PATHS,
}


class PreviewServer(ThreadingHTTPServer):
    request_queue_size = 64


class MediaSlice:
    def __init__(self, handle, length):
        self.handle = handle
        self.remaining = length

    def read(self, size):
        chunk = self.handle.read(min(size, self.remaining))
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        self.handle.close()


class FrontendHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_body(self, content, content_type, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        return BytesIO(content)

    def send_preview_reply(self, path):
        status, data = reply(self.command, path)
        return self.send_json(status, data)

    def send_json(self, status, data):
        return self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8", status)

    def local_hosts(self):
        port = self.server.server_port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def chat_allowed(self):
        origin = self.headers.get("Origin")
        invite = self.server.invitation
        if invite:
            return (self.headers.get("Host") == invite.host
                    and (not origin or origin == invite.origin)
                    and invite.session(self))
        host = self.headers.get("Host")
        return (
            host in self.local_hosts()
            and (not origin or origin == f"http://{host}")
            and self.headers.get("Authorization") == f"Bearer {commander.LOCAL_TOKEN}"
        )

    def redirect(self, destination, invitation=None, sign_out=False):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", destination)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        if invitation:
            invitation.set_cookie(self)
        if sign_out:
            self.send_header("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")
        self.end_headers()

    def serve_invite_page(self, status=HTTPStatus.OK):
        return self.send_body(login_page(status), "text/html; charset=utf-8", status)

    def send_head(self):
        target = urlsplit(self.path)
        path = target.path
        invite = self.server.invitation
        if invite:
            if self.headers.get("Host") != invite.host:
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "无效访问地址。"})
            if path in {"/invite", "/login"}:
                if invite.session(self):
                    return self.redirect(HOME_ROUTE)
                return self.serve_invite_page()
            if not invite.session(self):
                if not Path(path).suffix and not path.startswith(("/api/", "/agent-api/", "/scrm-api/", "/material-media/", "/ws/")):
                    return self.redirect("/invite")
                return self.send_json(HTTPStatus.UNAUTHORIZED, {"detail": "请先登录。"})
            if path == "/":
                return self.redirect(HOME_ROUTE)
            if images.MEDIA_PATH.fullmatch(path):
                return self.serve_generated_image(path)
            if path in STATIC_FILES or path.startswith(STATIC_DIRS):
                file = Path(self.translate_path(self.path)).resolve()
                if not file.is_relative_to(ROOT) or not file.is_file():
                    return self.send_json(HTTPStatus.NOT_FOUND, {"detail": "资源不存在。"})
            elif Path(path).suffix and path.lstrip("/").partition("/")[0] not in {"api", "agent-api", "scrm-api", "ws"}:
                return self.send_json(HTTPStatus.NOT_FOUND, {"detail": "资源不存在。"})
        if not invite and images.MEDIA_PATH.fullmatch(path):
            if self.headers.get("Host") not in self.local_hosts():
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "无效访问地址。"})
            return self.serve_generated_image(path)
        material_file = materials.FILE_PATH.fullmatch(path)
        if material_file:
            if not invite and self.headers.get("Host") not in self.local_hosts():
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "无效访问地址。"})
            return self.serve_material(material_file[1])
        contact_route = customers.CONTACT_PATH.fullmatch(path)
        if path in materials.READ or path in customers.READ or path == customer_import.TEMPLATE or (
            contact_route and not contact_route[2]
        ):
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "请先登录。"})
            try:
                if path == customer_import.TEMPLATE:
                    return self.send_body(customer_import.template(), customer_import.MIME)
                args = parse_qs(target.query)
                if path in materials.READ:
                    result = {"code": "0000", "data": materials.read(path, args)}
                else:
                    result = customers.read(path, args)
                return self.send_json(HTTPStatus.OK, result)
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"code": "INVALID", "msg": str(error), "detail": str(error)})
        image_option_paths = (
            images.MODELS_PATHS | images.EDIT_OPTIONS_PATHS | images.TRYON_OPTIONS_PATHS
            | images.PROMPT_LIBRARY_PATHS | images.FOLLOWUP_PATHS
        )
        if path in image_option_paths:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请先登录。"})
            query = parse_qs(target.query)
            if path in images.MODELS_PATHS:
                data = image_studio.designer_models()
            elif path in images.EDIT_OPTIONS_PATHS:
                data = image_studio.edit_options(query)
            elif path in images.TRYON_OPTIONS_PATHS:
                data = image_studio.tryon_options()
            elif path in images.PROMPT_LIBRARY_PATHS:
                data = {"list": []}
            else:
                data = {"ok": False, "error": "暂无后续结果。"}
            return self.send_json(HTTPStatus.OK, data)
        work_detail = images.WORK_DETAIL.fullmatch(path)
        if path in {"/api/works/list", "/agent-api/api/generation/list",
                    "/agent-api/api/generation/count"} or work_detail:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请先登录。"})
            try:
                result = images.detail(work_detail[1]) if work_detail else images.list_works(parse_qs(target.query))
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"msg": str(error)})
            if path.startswith("/api/"):
                result = {"code": "0000", "data": result}
            elif path.endswith("/count"):
                result = {"count": result["total"]}
            return self.send_json(HTTPStatus.OK, result)
        if path in operations.READ or operations.PROJECT_PATH.fullmatch(path):
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "请从本地运营页面打开。"})
            try:
                return self.send_json(HTTPStatus.OK, operations.read(path, parse_qs(target.query)))
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.NOT_FOUND, {"detail": str(error)})
            except ValueError:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"detail": "查询参数无效。"})
        if path in knowledge.READ_PATHS:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请从本地知识库打开。"})
            try:
                data = knowledge.read(path, parse_qs(target.query))
                return self.send_json(HTTPStatus.OK, {"code": "0000", "data": data})
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.NOT_FOUND, {"msg": str(error)})
        if path in memory.READ_PATHS:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "请从本地指挥官打开记忆。"})
            return self.send_json(HTTPStatus.OK, memory.read(path))
        inspiration = INSPIRATION_DETAIL.fullmatch(path)
        if path == inspirations.LIST or inspiration:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"detail": "请从本地灵感库打开。"})
            try:
                data = inspirations.detail(int(inspiration[1])) if inspiration else inspirations.listing(parse_qs(target.query))
                return self.send_json(HTTPStatus.OK, data)
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.NOT_FOUND, {"detail": str(error)})
        if path in CHAT_READ_PATHS:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请从本地指挥官打开对话。"})
            query = parse_qs(target.query)
            try:
                if path.endswith("/conversations"):
                    data = commander.history(query.get("source", [""])[0], query.get("keyword", [""])[0])
                else:
                    data = dispatch.attach_saved_plans(
                        commander.load_conversation(commander.conversation_id(query.get("id", [""])[0]))
                    )
                return self.send_json(HTTPStatus.OK, {"code": "0000", "data": data})
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.NOT_FOUND, {"msg": str(error)})
        if path in payments.READ_PATHS:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请先登录。"})
            try:
                data = payments.read(path, parse_qs(target.query))
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            return self.send_json(HTTPStatus.OK, {"code": "0000", "msg": "", "data": data})
        if geo.readable(path):
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请先登录。"})
            try:
                data = geo.read(path, parse_qs(target.query))
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            return self.send_json(HTTPStatus.OK, data)
        if path in acquisition.READ_PATHS:
            if not self.chat_allowed():
                return self.send_json(HTTPStatus.FORBIDDEN, {"msg": "请先登录。"})
            try:
                data = acquisition.read(path, parse_qs(target.query))
            except commander.ChatError as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            return self.send_json(HTTPStatus.OK, data)
        if path.lstrip("/").partition("/")[0] in {"api", "agent-api", "scrm-api", "material-media", "ws"}:
            return self.send_preview_reply(path)
        if path in self.server.studio_assets:
            return self.send_body(self.server.studio_assets[path], "text/javascript; charset=utf-8")
        if path in {"/", "/index.html"} or (
            not Path(path).suffix and not Path(self.translate_path(self.path)).exists()
        ):
            content = self.server.index_html
            if path != "/login":
                content = content.replace(b"<head>", b"<head>" + self.server.bootstrap + self.server.studio_import_map, 1)
            return self.send_body(content, "text/html; charset=utf-8")
        return super().send_head()

    def do_POST(self):
        self.close_connection = True
        path = urlsplit(self.path).path
        invite = self.server.invitation
        if invite:
            if self.headers.get("Host") != invite.host:
                return self.send_chat_json(HTTPStatus.FORBIDDEN, {"detail": "无效访问地址。"})
            if self.command == "POST" and path == "/invite":
                status = invite.authenticate(self)
                if status == HTTPStatus.SEE_OTHER:
                    return self.redirect(HOME_ROUTE, invite)
                with self.serve_invite_page(status) as response:
                    return self.copyfile(response, self.wfile)
            if not invite.session(self):
                return self.send_chat_json(HTTPStatus.UNAUTHORIZED, {"detail": "请先登录。"})
            if self.headers.get("Origin") != invite.origin:
                return self.send_chat_json(HTTPStatus.FORBIDDEN, {"detail": "请求来源无效。"})
            if self.command == "POST" and path == "/sign-out":
                return self.redirect("/invite", sign_out=True)
        contact_route = customers.CONTACT_PATH.fullmatch(path)
        post_routes = materials.WRITE | customers.WRITE | {materials.UPLOAD, customer_import.PREVIEW, customer_import.CONFIRM}
        if (self.command == "POST" and path in post_routes) or (contact_route and
            ((contact_route[2] and self.command == "POST") or
             (not contact_route[2] and self.command in {"PUT", "DELETE"}))
        ):
            return self.write_workspace(path)
        if ((self.command == "POST" and path in operations.POST)
                or (self.command == "PUT" and operations.REPORT_PATH.fullmatch(path))):
            self.handle_chat_post(path)
            return
        inspiration_use = re.fullmatch(re.escape(inspirations.PREFIX) + r"([0-9]{1,16})/use", path)
        if self.command == "POST" and (path in CHAT_WRITE_PATHS or path in memory.WRITE_PATHS
                                       or path in knowledge.WRITE_PATHS or inspiration_use):
            self.handle_chat_post(path)
            return
        if self.command in {"POST", "PUT", "DELETE"} and geo.writable(path):
            self.handle_chat_post(path)
            return
        with self.send_preview_reply(path) as response:
            self.copyfile(response, self.wfile)

    do_PUT = do_PATCH = do_DELETE = do_POST

    def send_chat_json(self, status, data):
        with self.send_json(status, data) as response:
            self.copyfile(response, self.wfile)

    def write_workspace(self, path):
        if not self.chat_allowed():
            return self.send_chat_json(HTTPStatus.FORBIDDEN, {"detail": "请先登录。"})
        large = path in {materials.UPLOAD, customer_import.PREVIEW}
        if large and not UPLOAD_SLOTS.acquire(blocking=False):
            return self.send_chat_json(HTTPStatus.SERVICE_UNAVAILABLE, {"detail": "上传繁忙，请稍后重试。"})
        try:
            limit = {materials.UPLOAD: materials.MAX_FILE + 64 * 1024,
                     customer_import.PREVIEW: customer_import.MAX_SIZE}.get(path, 160_000)
            size = int(self.headers.get("Content-Length", "0"))
            if size > limit:
                return self.send_chat_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"detail": "文件或请求超出大小限制。"})
            if size < 0:
                raise ValueError()
            self.connection.settimeout(45)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            kind = self.headers.get_content_type()
            if path == materials.UPLOAD and kind == "multipart/form-data":
                result = {"code": "0000", "data": materials.upload(self.headers["Content-Type"], raw)}
            elif path == customer_import.PREVIEW and kind == customer_import.MIME:
                result = customer_import.preview(raw)
            elif not large and (not size or kind == "application/json"):
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError()
                if path in materials.WRITE:
                    result = {"code": "0000", "data": materials.write(path, body)}
                elif path == customer_import.CONFIRM:
                    result = customer_import.confirm(body)
                else:
                    result = customers.write(self.command, path, body)
            else:
                raise ValueError()
            self.send_chat_json(HTTPStatus.OK, result)
        except (ValueError, UnicodeError, commander.ChatError) as error:
            message = str(error) if isinstance(error, commander.ChatError) else "请求格式无效。"
            self.send_chat_json(HTTPStatus.BAD_REQUEST, {"code": "INVALID", "msg": message, "detail": message})
        except TimeoutError:
            self.send_chat_json(HTTPStatus.REQUEST_TIMEOUT, {"detail": "读取文件超时。"})
        finally:
            if large:
                UPLOAD_SLOTS.release()

    def serve_material(self, key):
        record = materials.connection_file(key, registered_only=False)
        if record is None:
            return self.send_json(HTTPStatus.NOT_FOUND, {"detail": "素材不存在。"})
        try:
            handle = (materials.directory() / key).open("rb")
        except FileNotFoundError:
            return self.send_json(HTTPStatus.NOT_FOUND, {"detail": "素材不存在。"})
        size = record["size"]
        start, end, status = 0, size - 1, HTTPStatus.OK
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=([0-9]{0,12})-([0-9]{0,12})", requested)
            if match and match[1]:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            elif match and match[2] and int(match[2]) > 0:
                start = max(0, size - int(match[2]))
            else:
                start = size
            if not start <= end < size:
                handle.close()
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", record["mime_type"])
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        disposition = "attachment" if record["media_type"] == "document" else "inline"
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quote(record['name'], safe='')}")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        handle.seek(start)
        return MediaSlice(handle, end - start + 1)

    def handle_chat_post(self, path):
        if not self.chat_allowed():
            self.send_chat_json(HTTPStatus.FORBIDDEN, {"msg": "请从本地指挥官发送消息。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 160_000 or self.headers.get_content_type() != "application/json":
                raise commander.ChatError("对话请求格式无效或体积过大。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise commander.ChatError("对话请求格式无效。")
            if path == operations.LAUNCH:
                self.start_stream()
                try:
                    operations.launch(body, self.emit)
                except commander.ChatError as error:
                    self.emit("error", message=str(error))
                return
            if path in operations.POST or operations.REPORT_PATH.fullmatch(path):
                try:
                    result = operations.write(path, body)
                except commander.ChatError as error:
                    result = {"ok": False, "error": str(error)}
                self.send_chat_json(HTTPStatus.OK, result)
                return
            if path in knowledge.WRITE_PATHS:
                self.send_chat_json(HTTPStatus.OK, {"code": "0000", "data": knowledge.write(path, body)})
                return
            if path in memory.WRITE_PATHS:
                self.send_chat_json(HTTPStatus.OK, memory.write(path, body))
                return
            if path in acquisition.WRITE_PATHS:
                try:
                    result = acquisition.write(path, body)
                except commander.ChatError as error:
                    self.send_chat_json(HTTPStatus.BAD_REQUEST, {"code": "INVALID", "msg": str(error), "detail": str(error)})
                    return
                self.send_chat_json(HTTPStatus.OK, result)
                return
            if path in digital_human.WRITE_PATHS:
                try:
                    result = digital_human.write(path, body)
                except commander.ChatError as error:
                    self.send_chat_json(HTTPStatus.BAD_REQUEST, {"code": "INVALID", "msg": str(error), "detail": str(error)})
                    return
                self.send_chat_json(HTTPStatus.OK, result)
                return
            if geo.writable(path):
                try:
                    result = geo.write(self.command, path, body)
                except commander.ChatError as error:
                    self.send_chat_json(HTTPStatus.BAD_REQUEST, {"code": "INVALID", "msg": str(error), "detail": str(error)})
                    return
                self.send_chat_json(HTTPStatus.OK, result)
                return
            if path in payments.WRITE_PATHS:
                try:
                    result = payments.write(path, body)
                except commander.ChatError as error:
                    self.send_chat_json(
                        HTTPStatus.BAD_REQUEST,
                        {"code": "INVALID", "msg": str(error), "detail": str(error)},
                    )
                    return
                self.send_chat_json(HTTPStatus.OK, {"code": "0000", "msg": "", "data": result})
                return
            if path == dispatch.PLAN_STEP:
                self.send_chat_json(HTTPStatus.OK, dispatch.update_step(body))
                return
            if path == inspirations.CREATE:
                saved = inspirations.save(body.get("title"), body.get("content"), "idea", "灵感",
                                          {}, "指挥官")
                self.send_chat_json(HTTPStatus.OK, saved)
                return
            if path.startswith(inspirations.PREFIX) and path.endswith("/use"):
                inspirations.detail(int(path.split("/")[-2]))
                self.send_chat_json(HTTPStatus.OK, {"ok": True})
                return
            if (path in images.STREAM_PATHS or path in images.EDIT_STREAM_PATHS
                    or path in images.PRODUCT_BG_STREAM_PATHS
                    or path in images.TRYON_STREAM_PATHS):
                self.stream_image(path, body)
                return
            if path == studio.GUIDE_PATH:
                self.send_chat_json(HTTPStatus.OK, studio.guide(body))
                return
            if path == studio.STREAM_PATH:
                parent = body.get("from_inspiration_id")
                self.stream_studio(list(studio.requests(body)), parent if type(parent) is int and parent > 0 else None)
                return
            if path in {"/api/chat/send", commander.COMMAND_STREAM}:
                command = path == commander.COMMAND_STREAM
                key, messages, user_text = commander.parse_chat(body, command)
                messages = knowledge.context(messages, body)
                source = "" if command else str(body.get("source") or "")[:100]
                self.stream_chat(key, messages, user_text, source, command)
                return
            if path.endswith("/delete"):
                if not body.get("id"):
                    raise commander.ChatError("缺少会话编号。")
                commander.delete_conversation(commander.conversation_id(body["id"]))
                data = {}
            elif path.endswith("/skill/start") and body.get("progress_hint") == commander.COMMAND_PROGRESS:
                data = {"conversation_id": commander.conversation_id(body.get("conversation_id"))}
            elif path.endswith("/persist-skill") and body.get("skill_label") == "起号向导":
                key = commander.conversation_id(body.get("conversation_id"))
                user_text = str(body.get("user_text") or "").strip()[:500]
                reply_text = str(body.get("assistant_text") or "").strip()[:8000]
                if user_text and reply_text:
                    wizard = None
                    if body.get("project_id"):
                        wizard = operations.restore_card(body["project_id"], operations.board_date(body.get("board_date")))
                    commander.save_turn(key, user_text, reply_text, str(body.get("source") or "")[:100], wizard=wizard)
                data = {"conversation_id": key}
            elif path.endswith("/persist-skill") and body.get("skill_label") == "指挥调度":
                key = commander.conversation_id(body.get("conversation_id"))
                commander.load_conversation(key)
                data = {"conversation_id": key}
            else:
                with self.send_preview_reply(path) as response:
                    self.copyfile(response, self.wfile)
                return
            self.send_chat_json(HTTPStatus.OK, {"code": "0000", "data": data})
        except commander.ChatError as error:
            self.send_chat_json(HTTPStatus.BAD_REQUEST, {"msg": str(error)})
        except (ValueError, UnicodeError):
            self.send_chat_json(HTTPStatus.BAD_REQUEST, {"msg": "对话请求格式无效。"})
        except (BrokenPipeError, ConnectionResetError):
            return

    def serve_generated_image(self, path):
        filename = images.MEDIA_PATH.fullmatch(path)[1]
        file = images.file_for(filename)
        if not file:
            return self.send_json(HTTPStatus.NOT_FOUND, {"detail": "图片不存在。"})
        handle = file.open("rb")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png" if file.suffix == ".png" else "image/jpeg")
        self.send_header("Content-Length", str(file.stat().st_size))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        return handle

    def stream_image(self, path, body):
        canonical = path[len("/agent-api"):] if path.startswith("/agent-api") else path
        if canonical in images.STREAM_PATHS:
            request = images.validate(body)
        else:
            try:
                request = image_studio.build(canonical, body)
            except ValueError as error:
                raise commander.ChatError(str(error)) from error
        def local_media(value):
            if value.startswith("/material-media/"):
                return materials.uploaded_path(value[len("/material-media/"):])
            if value.startswith("/generated-media/"):
                return images.file_for(value[len("/generated-media/"):])
            return None

        def absolute(value):
            if not isinstance(value, str):
                return value
            target = urlsplit(value)
            if target.scheme in {"http", "https"}:
                if target.netloc in self.local_hosts():
                    found = local_media(target.path)
                    if found:
                        return found
                return value
            found = local_media(value)
            if found:
                return found
            raise commander.ChatError("图片地址无效。")

        try:
            request["images"] = [absolute(item) for item in request.get("images") or []]
            if request.get("mask"):
                request["mask"] = absolute(request["mask"])
        except commander.ChatError:
            raise
        except (TypeError, ValueError) as error:
            raise commander.ChatError("图片地址无效。") from error
        if not images.ACTIVE.acquire(blocking=False):
            raise commander.ChatError("正在处理另一张图片，请稍后再试。")
        try:
            images.reserve()
            def run():
                try:
                    content, meta = images.generate(request)
                    work_id, url = images.save(content, request["prompt"], request.get("model") or images.MODEL)
                    return work_id, url, meta
                finally:
                    images.ACTIVE.release()
            job = self.server.image_pool.submit(run)
        except BaseException:
            images.ACTIVE.release()
            raise
        self.start_stream()
        try:
            self.emit("delta", content="正在生成图片…", transient=True)
            while True:
                try:
                    work_id, url, meta = job.result(timeout=7)
                    break
                except JobTimeout:
                    self.emit("delta", content="正在等待模型返回图片…", transient=True)
            model_name = request.get("model") or images.MODEL
            self.emit("data", data={
                "image_url": url, "work_id": work_id, "model_name": model_name,
                "src_size": request.get("size"), "out_size": request.get("size"),
            })
            self.emit("usage", model=model_name, at=int(time.time() * 1000))
            self.emit("done")
        except (BrokenPipeError, ConnectionResetError):
            return
        except commander.ChatError as error:
            self.emit("error", message=str(error))
        except (OSError, ValueError):
            self.emit("error", message="图片生成或保存失败，请重试。")

    def start_stream(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def emit(self, kind, **values):
        event = json.dumps({"type": kind, **values}, ensure_ascii=False)
        self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
        self.wfile.flush()

    def stream_studio(self, requests, parent):
        self.start_stream()
        try:
            for platform, goal, messages in requests:
                name = studio.PLATFORMS[platform][0]
                try:
                    with closing(commander.generate(messages)) as tokens:
                        markdown = "".join(tokens).strip()
                    if not markdown:
                        raise commander.ChatError("OpenAI 没有返回文案，请重试。")
                except commander.ChatError as error:
                    self.emit("error", message=f"{name}：{error}")
                    continue
                data = studio.result(markdown, platform, goal)
                saved = inspirations.save(data["content"]["title"], markdown, data["output"], studio.GOALS[goal][0],
                                          {"content": data["content"], "platform": platform}, name, parent)
                data["inspiration_id"] = saved["id"]
                self.emit("data", data=data)
                self.emit("usage", model=commander.MODEL)
            self.emit("done")
        except (BrokenPipeError, ConnectionResetError):
            return
        except commander.ChatError as error:
            self.emit("error", message=str(error))
        except (OSError, ValueError):
            self.emit("error", message="文案生成中断，请重试。")

    def stream_chat(self, key, messages, user_text, source, command):
        self.start_stream()
        text_event = "delta" if command else "content"
        fragments = []

        try:
            actions = dispatch.actions(user_text, self.server.dispatch_catalog) if command else []
            if actions:
                if len(actions) == 1:
                    action = actions[0]
                    notice = f"正在打开{action['name']}。"
                    if action.get("prefill"):
                        notice = "已把需求交给内容创作台。信息齐全时会直接出稿，缺少的信息可以在需求单里补充。"
                    commander.save_turn(key, user_text, notice, source)
                    self.emit("meta", intent="action")
                    self.emit("delta", content=notice)
                    self.emit("action", **action)
                else:
                    plan = dispatch.create_plan(key, actions)
                    notice = "步骤已排好。可以从第一步开始，并在完成后推进到下一步。"
                    commander.save_turn(key, user_text, notice, source, plan["task_id"])
                    self.emit("meta", intent="plan")
                    self.emit("delta", content=notice)
                    self.emit("plan", **plan)
                self.emit("done", conversation_id=key)
                return
            if command:
                self.emit("meta", intent="chat")
            with closing(commander.generate(memory.context(messages))) as tokens:
                for fragment in tokens:
                    fragments.append(fragment)
                    self.emit(text_event, content=fragment)
            content = "".join(fragments)
            if not content.strip():
                raise commander.ChatError("OpenAI 没有返回可显示的文字，请重试。")
            commander.save_turn(key, user_text, content, source)
            self.emit("done", conversation_id=key)
        except (BrokenPipeError, ConnectionResetError):
            return
        except commander.ChatError as error:
            self.emit("error", message=str(error))
        except (OSError, ValueError):
            self.emit("error", message="对话连接中断，请重试。")

    def end_headers(self):
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' blob:; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://api.qrserver.com "
            "https://aiagent-1451213911.cos.ap-guangzhou.myqcloud.com; "
            "font-src 'self' data:; media-src 'self' blob: data:; worker-src 'self' blob:; "
            "manifest-src 'self' blob:; "
            "connect-src 'self'; form-action 'self'; object-src 'none'; base-uri 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.server.invitation:
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()


def prepare_archived_preview(server):
    server.studio_assets = studio.load_frontend_assets(ROOT)
    server.studio_assets.update(prepare_manual_publishing(ROOT))
    imports = {path: f"{path}?ev-workspace=6" for path in server.studio_assets}
    brand = BRAND_NAME.encode("utf-8")
    server.index_html = (ROOT / "index.html").read_bytes().replace(
        b"<title>AgentMax</title>", b"<title>" + brand + b"</title>", 1
    )
    # Keep the archived build intact and brand the fallback name it serves locally.
    entry = re.search(rb'src="(/assets/index-[^"]+\.js)"', server.index_html)
    if entry:
        entry_path = entry.group(1).decode("ascii")
        source = (ROOT / entry_path.lstrip("/")).read_bytes()
        default_name = b'"' + brand + b'"'
        commander_label = 'key:"marketing-brain",label:"指挥官"'.encode("utf-8")
        source = source.replace(b'"AgentMax"', default_name, 1).replace(
            commander_label, b'key:"marketing-brain",label:' + default_name, 1
        )
        if server.invitation:
            logout_start = b'function C(){e.value="",t.value={id:0,nickname:"",avatar:"",mobile:""}'
            logout = (b'function C(){localStorage.removeItem("token");'
                      b'fetch("/sign-out",{method:"POST"}).then(r=>{'
                      b'if(!r.ok)throw Error("Logout failed");'
                      b'location.replace("/invite")});return;'
                      + logout_start[len(b'function C(){'):])
            if source.count(logout_start) != 1:
                raise RuntimeError("Hosted logout does not match the archived entry.")
            source = source.replace(logout_start, logout, 1)
        server.studio_assets[entry_path] = source
    server.dispatch_catalog = dispatch.load_catalog(ROOT)
    server.studio_import_map = (
        '<script type="importmap">' + json.dumps({"imports": imports}) + "</script>"
    ).encode("utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    origin = os.environ.get("EV_TRIAL_ORIGIN")
    if not (ROOT / "index.html").is_file():
        parser.error("Run fetch_frontend.py first.")
    if origin and not (os.environ.get("EV_INVITE_CONFIG") and os.environ.get("EV_DATA_DIR")):
        parser.error("Invite mode requires EV_INVITE_CONFIG and a separate EV_DATA_DIR.")
    invite = Invitation(origin, os.environ["EV_INVITE_CONFIG"]) if origin else None
    if invite:
        invite.credentials()
    with PreviewServer(("127.0.0.1", args.port), FrontendHandler) as server:
        server.invitation = invite
        server.image_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="designer")
        server.bootstrap = BOOTSTRAP
        if invite:
            server.bootstrap = BOOTSTRAP.replace(
                b"location.hostname === '127.0.0.1'", b"true", 1
            )
        prepare_archived_preview(server)
        print(f"http://127.0.0.1:{server.server_port}/mb/home", flush=True)
        server.serve_forever()
