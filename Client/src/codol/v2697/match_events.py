"""2.6.9.7 战斗断开事件分类，区分可恢复断线和真正的引擎故障。"""


def is_battle_disconnect(arguments, online):
    """只在线模式允许已知断开码继续原生回房；资源/脚本/未知错误仍停止。

这是允许恢复的事件，不代表本局成功。对局结束原因和验收结论仍由服务端
状态及回房/再次开局证据决定，不能用此分类掩盖意外断服。
"""
    # EXE_MATCHENDED：服务端对局结束（exitLevel 的可靠 t 命令）下发的良性
    # 离场通知，客户端按原生旁路理由静默回前台——不是引擎故障。M3 停止链路
    # 由服务端 sh1x_endmatch 主动发送它，必须允许客户端走完原生回房流程。
    # EXE_ERR_SERVER_TIMEOUT 等仍是真实故障，保持严格失败。
    return bool(online and len(arguments) > 1 and arguments[1] in (
        'EXE_DISCONNECTED', 'EXE_SERVER_DISCONNECTED', 'EXE_SERVERQUIT',
        'EXE_MATCHENDED'))
