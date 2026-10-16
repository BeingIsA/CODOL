"""管理房间查询：读取玩家用例的同一权威快照，不维护第二份状态。"""
from .ports import RoomQueries

class RoomService:
    """管理页只读入口；写入必须走明确认证的命令。"""
    def __init__(self, rooms: RoomQueries):
        """注入权威查询源，替代 M1 的恒空占位。"""
        self._rooms = rooms

    def list_rooms(self):
        """返回实际快照和即时在线状态，不生成占位房间。"""
        return self._rooms.list_rooms()
