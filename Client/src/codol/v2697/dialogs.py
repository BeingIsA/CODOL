# Version 2.6.9.7 research port; see docs/provenance.json.
"""版本适配层：客户端弹窗文本分类（仅 2.6.9.7 中文文案）。

调试会话会拦下客户端的所有 MessageBox。只有零售版"硬件变更确认"
弹窗允许放行（用户可自行选择是否优化），其余一律按错误处理并在
展示前截获。
"""


def classify(caption, message, flags):
    """按标题/正文/按钮标志分类弹窗，返回 'confirmation' 或 'error'。

    confirmation 仅匹配客户端原文的"进行优化设置？"硬件变更确认
    （电脑硬件变化 + 新硬件优化配置 + 是/否按钮组合）。
    """
    text = ''.join(message.split())
    hardware = '电脑' in text and '上一次运行' in text and ('变化' in text)
    optimize = '新硬件' in text and '优化配置' in text and ('操作设定' in text)
    if caption.strip() == '进行优化设置？' and flags & 255 == 68 and ('CallofDutyOnline' in text or 'CODOL' in text) and hardware and optimize:
        return 'confirmation'
    return 'error'
