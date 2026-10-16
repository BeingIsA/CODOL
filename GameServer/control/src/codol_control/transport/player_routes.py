"""M2 玩家 API 与 SSE 推送：身份与管理员路由分开，令牌不放 URL。

玩家通过命令 HTTP 写入，SSE 单向推送快照/事件；不需要第三方 WebSocket
依赖。每轮推送重新验证会话，撤销、过期和停服都会结束旧连接。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .routes import session_payload
from ..domain.errors import ControlError, UnauthenticatedError

router = APIRouter()


def player_token(authorization: str | None = Header(default=None)) -> str:
    """玩家凭据只接受 Bearer 头，管理员令牌不能自动升级为玩家。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthenticatedError("需要玩家会话。")
    return authorization[7:]


class Login(BaseModel):
    """有明确开发标识的身份引导，不能当作官方账号登录。"""
    identity: str = Field(min_length=32, max_length=128)
    display_name: str = Field(min_length=1, max_length=24)
    protocol_version: int = 1


class Create(BaseModel):
    """幂等建房请求；不接收可伪造的玩家身份字段。"""
    request_id: str = Field(min_length=1, max_length=96)
    map: str = "mp_cross_fire"
    gametype: str = "war"


class Leave(BaseModel):
    """离房同样要求请求编号，网络重试不会多次修改。"""
    request_id: str = Field(min_length=1, max_length=96)
    room_id: str


class Bots(BaseModel):
    """管理员提交的 Bot 席位配置，状态与版本由领域再次校验。"""
    request_id: str = Field(min_length=1, max_length=96)
    expected_room_revision: int = Field(ge=1)
    count: int = Field(ge=0, le=11, strict=True)
    team: int = Field(ge=1, le=2)
    difficulty: str


class ChangeRoom(BaseModel):
    """原生配置意图；仅允许本人阵营和房主地图，权限由用例校验。"""
    request_id: str = Field(min_length=1, max_length=96)
    room_id: str
    expected_room_revision: int = Field(ge=1)
    map: str | None = None
    team: int | None = Field(default=None, ge=1, le=2)


class MatchStart(BaseModel):
    """M3 原生开局意图；只有房主身份可以通过用例校验。"""
    request_id: str = Field(min_length=1, max_length=96)
    room_id: str


class MatchCancel(MatchStart):
    """取消必须指定开局回执中的对局 ID，旧请求不能影响新一局。"""
    match_id: str = Field(min_length=1)


@router.post("/player/room-config")
def change_room(body: ChangeRoom, request: Request, token=Depends(player_token)):
    """接收原生选图/改队请求，返回新版本后才能更新客户端投影。"""
    return request.app.state.container.player_rooms.change(
        token, body.request_id, body.room_id, body.expected_room_revision, map_name=body.map, team=body.team)


@router.post("/player/login")
def login(body: Login, request: Request, x_development_key: str = Header(default="")):
    """校验本机开发接入凭据后签发独立玩家会话。"""
    return request.app.state.container.player_rooms.login(
        x_development_key, body.identity, body.display_name, body.protocol_version)


@router.post("/player/heartbeat")
def heartbeat(request: Request, token=Depends(player_token)):
    """刷新玩家租约；管理页随查询即时判断是否超时。"""
    return request.app.state.container.player_rooms.heartbeat(token)


@router.post("/player/disconnect")
def disconnect(request: Request, token=Depends(player_token)):
    """主动关闭立即离线，不依赖后台定时器。"""
    return request.app.state.container.player_rooms.disconnect(token)


@router.post("/player/rooms")
def create(body: Create, request: Request, token=Depends(player_token)):
    """只通过玩家身份创建权威房间。"""
    return request.app.state.container.player_rooms.create(token, body.request_id, body.map, body.gametype)


@router.post("/player/leave")
def leave(body: Leave, request: Request, token=Depends(player_token)):
    """成员退出；当前单真人策略为关闭房间并保留历史。"""
    return request.app.state.container.player_rooms.leave(token, body.request_id, body.room_id)


@router.post("/player/match-start")
def match_start(body: MatchStart, request: Request, token=Depends(player_token)):
    """房主从原生房间发起开局；进程启动与就绪由服务端异步推进。"""
    return request.app.state.container.matches.start("player", token, body.room_id, body.request_id)


@router.get("/player/snapshot")
def snapshot(request: Request, after: int = 0, token=Depends(player_token)):
    """完整同步入口；重连不依赖本地缓存状态。"""
    return request.app.state.container.player_rooms.sync(token, after)


@router.post("/player/match-cancel")
def match_cancel(body: MatchCancel, request: Request, token=Depends(player_token)):
    """玩家取消准备复用优雅停局流程；权限和幂等回执由用例管理。"""
    return request.app.state.container.matches.cancel_player(
        token, body.room_id, body.match_id, body.request_id)


@router.get("/player/events")
async def events(request: Request, after: int = 0, token=Depends(player_token)):
    """SSE 推送持久化事件及完整快照；心跳帧兼作连接健康反馈。"""
    service = request.app.state.container.player_rooms
    first = await asyncio.to_thread(service.sync, token, after)

    async def stream():
        """事件循环只等待，数据库操作在线程池，不阻塞其他玩家和停服。"""
        state = first
        while not await request.is_disconnected():
            yield "event: sync\ndata: " + json.dumps(state, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(2)
            if request.app.state.container.uvicorn_server.should_exit:
                return
            try:
                state = await asyncio.to_thread(service.sync, token, state["cursor"])
            except ControlError:
                yield 'event: expired\ndata: {}\n\n'
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/rooms/{room_id}/bots")
def bots(room_id: str, body: Bots, request: Request, admin=Depends(session_payload)):
    """管理员认证独立于玩家认证；玩家令牌不能修改 Bot 配置。"""
    return request.app.state.container.player_rooms.bots(
        room_id, body.request_id, body.expected_room_revision, body.count, body.team, body.difficulty)
