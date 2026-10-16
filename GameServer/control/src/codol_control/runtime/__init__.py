"""运行时层：契约加载、结构化探测、战斗进程监督与测试替身。

M1 的“运行时管理”= 只读探测（契约 §2 预检）+ 能力报告；
M3 起进程监督（launch/status/stop/collect/adopt_or_kill）由
supervisor.NativeSupervisor 按同一契约实现，进程与套接字只允许出现在本层。
"""
