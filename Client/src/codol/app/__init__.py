"""应用层：会话的编排与装配。

本包把"一次本地大厅会话"拆成三个关注点：
- ``session_lock``  单会话互斥锁（同一时刻只允许一个会话）；
- ``context``       会话装配（账号目录、资源提供器、参数对象）；
- ``lifecycle``     check/stage/launch/smoke/stop 各命令的执行体。
"""
