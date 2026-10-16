"""API 路由：登录会话、总览、能力、房间、运行时、诊断、停服。

约定：公开端点仅 /health 与 /capabilities（无敏感数据）；
其余全部要求 Bearer 会话令牌；写操作与停服另有说明。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from ..domain.errors import UnauthenticatedError

router = APIRouter()


def _container(request: Request):
    """从应用状态取容器（装配由 create_app 完成）。"""
    return request.app.state.container


def session_payload(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """鉴权依赖：解析 Bearer 令牌并校验，失败抛 401 领域错误。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthenticatedError("缺少会话令牌（Bearer）。")
    return _container(request).admin.verify_token(authorization[len("Bearer "):])


# ---- 公开端点 -----------------------------------------------------------


@router.get("/health")
def health(request: Request):
    """存活探测：不鉴权、无敏感字段，供停服脚本与监控使用。"""
    c = _container(request)
    return {"status": "ok", "version": c.version, "boot_id": c.boot_id}


@router.get("/capabilities")
def capabilities(request: Request):
    """能力表：当前版本支持/不支持的功能，前端与未来客户端据此协商。"""
    c = _container(request)
    return {
        "service_version": c.version,
        "features": {
            "admin_ui": True,
            "admin_auth": True,
            "runtime_probe": True,     # 契约预检（只读）
            "rooms_list": True,
            "rooms_create": True,      # M2：服务端 API 已实现；原生 UI 验收单列
            "rooms_close": True,       # 管理页：管理员强制关闭房间（含对局中先停局）
            "player_channel": True,    # M2：开发身份 + HTTP 命令 / SSE 推送
            "match_control": not c.matches.fake,  # M3：开局/停止（测试假监督器时为 False）
            "bots": False,             # M4
        },
        "max_active_rooms": 1,
        "milestone": "M3-development",
    }


# ---- 会话与管理员 --------------------------------------------------------


class LoginBody(BaseModel):
    """登录请求体。"""

    username: str
    password: str


@router.post("/admin/login")
def admin_login(body: LoginBody, request: Request):
    """口令登录换取会话令牌；失败统一 401。"""
    return _container(request).admin.login(body.username, body.password)


@router.get("/admin/session")
def admin_session(payload: dict = Depends(session_payload)):
    """当前会话信息（whoami）。"""
    return {"username": payload.get("sub"), "expires_at": _dt_of(payload)}


class ChangePasswordBody(BaseModel):
    """改密请求体。"""

    old_password: str
    new_password: str


@router.post("/admin/password")
def change_password(body: ChangePasswordBody, request: Request,
                    payload: dict = Depends(session_payload)):
    """修改管理员密码；成功后旧令牌全部失效（客户端需重新登录）。"""
    _container(request).admin.change_password(payload, body.old_password, body.new_password)
    return {"changed": True}


@router.post("/admin/shutdown")
def shutdown(request: Request, authorization: str | None = Header(default=None),
             x_shutdown_token: str | None = Header(default=None)):
    """优雅停服：管理员会话或本机停服令牌（Stop.cmd）二选一。

    只置位退出标志，由主循环完成收尾（关库、删控制文件、写日志），
    路由本身立即返回，避免连接被硬切断。
    """
    c = _container(request)
    payload = None
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = c.admin.verify_token(authorization[len("Bearer "):])
        except UnauthenticatedError:
            payload = None
    c.admin.authorize_shutdown(payload, x_shutdown_token, c.shutdown_token)
    c.request_graceful_shutdown()
    return {"shutting_down": True, "boot_id": c.boot_id}


# ---- 业务查询 -----------------------------------------------------------


@router.get("/overview")
def overview(request: Request, payload: dict = Depends(session_payload)):
    """总览数据；bind 字段在此补充实际监听地址。"""
    c = _container(request)
    data = c.overview.build()
    data["service"]["bind"] = f"{c.config.bind_host}:{c.config.bind_port}"
    return data


@router.get("/rooms")
def rooms(request: Request, payload: dict = Depends(session_payload)):
    """房间列表（M1 恒空）与容量说明。"""
    return _container(request).rooms.list_rooms()


@router.get("/runtime")
def runtime_status(request: Request, payload: dict = Depends(session_payload)):
    """运行时缓存状态与活动对局事实；探测动作见 POST /runtime/probe。"""
    c = _container(request)
    probe = c.runtime.cached()
    matches = c.matches.status()
    return {
        "probe": _probe_dict(probe) if probe else None,
        "port_free": c.runtime.port_free(),
        "match_processes": matches["match_processes"],
        "matches": matches,
    }


@router.post("/runtime/probe")
def runtime_probe(request: Request, payload: dict = Depends(session_payload)):
    """触发深度探测（全量哈希，可能耗时几十秒）；结果进入缓存。"""
    c = _container(request)
    probe = c.runtime.probe(force=True)
    c.admin.audit(payload.get("sub", "?"), "runtime.probe", {"ready": probe.ready})
    return {"probe": _probe_dict(probe), "port_free": c.runtime.port_free()}


@router.post("/rooms/{room_id}/close")
def room_close(room_id: str, body: RoomCloseBody, request: Request,
               payload: dict = Depends(session_payload)):
    """管理员关闭房间：清空全部成员；有活动对局时先优雅停局再关房。

    与玩家 /player/leave 的区别：leave 只移除本人席位，关闭是整个房间的
    处置，房内所有人同时失去席位，客户端同步后会回到大厅。
    停止进程需要等待（最长约 20 秒），因此本路由是同步端点，由框架
    放到线程池执行，不阻塞事件循环。
    """
    c = _container(request)
    result = c.admin_rooms.close(room_id, body.request_id, body.reason or "管理员关闭房间")
    c.admin.audit(payload.get("sub", "?"), "room.close",
                  {"room_id": room_id, "revision": result["snapshot"]["revision"]})
    return result


# ---- M3 对局控制（管理员） -----------------------------------------------


class RoomCloseBody(BaseModel):
    """管理员关闭房间请求体；对局中房间会先优雅停局再关房。"""
    request_id: str
    reason: str = ""


class MatchStartBody(BaseModel):
    """管理员开局请求体；玩家开局走 /player/match-start（房主校验）。"""
    request_id: str


class MatchTerminateBody(BaseModel):
    """停止/取消请求体；语义区分见 MatchService.stop / cancel。"""
    request_id: str
    reason: str = ""


@router.post("/rooms/{room_id}/match/start")
def match_start(room_id: str, body: MatchStartBody, request: Request,
                payload: dict = Depends(session_payload)):
    """管理员从当前房间开局：冻结配置并启动战斗进程（异步就绪）。"""
    c = _container(request)
    result = c.matches.start("admin", None, room_id, body.request_id)
    c.admin.audit(payload.get("sub", "?"), "match.start",
                  {"room_id": room_id, "match_id": result.get("match_id")})
    return result


@router.get("/matches")
def matches_status(request: Request, payload: dict = Depends(session_payload)):
    """活动对局与最近对局状态。"""
    return _container(request).matches.status()


@router.post("/matches/{match_id}/stop")
def match_stop(match_id: str, body: MatchTerminateBody, request: Request,
               payload: dict = Depends(session_payload)):
    """按正常结束路径停止对局（FINISHED）。"""
    c = _container(request)
    result = c.matches.stop(match_id, body.request_id, body.reason or "管理员停止")
    c.admin.audit(payload.get("sub", "?"), "match.stop", {"match_id": match_id})
    return result


@router.post("/matches/{match_id}/cancel")
def match_cancel(match_id: str, body: MatchTerminateBody, request: Request,
                 payload: dict = Depends(session_payload)):
    """取消对局（CANCELLED）：启动中/进行中均可取消。"""
    c = _container(request)
    result = c.matches.cancel(match_id, body.request_id, body.reason or "管理员取消")
    c.admin.audit(payload.get("sub", "?"), "match.cancel", {"match_id": match_id})
    return result


# ---- 诊断 ---------------------------------------------------------------


@router.get("/diagnostics/logs")
def diagnostics_logs(request: Request, limit: int = 100,
                     payload: dict = Depends(session_payload)):
    """服务 JSONL 日志尾部（已脱敏）。"""
    return {"entries": _container(request).diagnostics.recent_log_entries(limit)}


@router.get("/diagnostics/server-runs")
def diagnostics_runs(request: Request, payload: dict = Depends(session_payload)):
    """原生运行历史（.server-runs）摘要，只读。"""
    return {"runs": _container(request).diagnostics.list_server_runs()}


@router.get("/diagnostics/bundle")
def diagnostics_bundle(request: Request, payload: dict = Depends(session_payload)):
    """一次性诊断包：日志尾部 + 运行历史 + 时间戳。"""
    return _container(request).diagnostics.bundle()


# ---- 工具 ---------------------------------------------------------------


def _probe_dict(probe) -> dict:
    """把 RuntimeProbeResult 转为响应字典（含 problems 人读清单）。"""
    return {
        "checked_at": probe.checked_at,
        "ready": probe.ready,
        "fake": probe.fake,
        "exe_found": probe.exe_found,
        "exe_sha256": probe.exe_sha256,
        "audit_passed": probe.audit_passed,
        "mirror_ok": probe.mirror_ok,
        "assets_total": probe.assets_total,
        "assets_verified": probe.assets_verified,
        "asset_failures": probe.asset_failures[:20],
        "supported_content": probe.supported_content,
        "contract_version": probe.contract_version,
        "problems": probe.problems,
    }


def _dt_of(payload: dict) -> str:
    """令牌过期时间转可读字符串（无则空串）。"""
    from datetime import datetime, timezone

    exp = payload.get("exp")
    if not exp:
        return ""
    return datetime.fromtimestamp(int(exp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
