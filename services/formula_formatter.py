"""数学公式显示美化。

从 EMath3DVisualizer 提取，适配为无 Qt 依赖的纯 Python 实现，
用于后端生成图例标签、前端展示方程文本。
"""
import re


# 数字上标映射
SUP_MAP = str.maketrans({
    '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
    '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
    '+': '⁺', '-': '⁻'
})


def sympy_to_label(eq_text: str) -> str:
    """将 SymPy 风格的方程文本转换为更美观的 Unicode 字符串。

    例如：
        x**2 + y**2 = 10   ->   x² + y² = 10
        sqrt(x+y)          ->   √(x+y)
        z <= x**2          ->   z ≤ x²
    """
    s = eq_text.strip().replace('**', '^')

    # 常见数学符号美化
    s = re.sub(r'\bsqrt\s*\(', '√(', s, flags=re.IGNORECASE)
    s = re.sub(r'\bpi\b', 'π', s, flags=re.IGNORECASE)

    # 上标转换
    def _to_sup(match: re.Match) -> str:
        return match.group(1).translate(SUP_MAP)

    s = re.sub(r'\^\(([-+]?\d+)\)', _to_sup, s)
    s = re.sub(r'\^([-+]?\d+)', _to_sup, s)

    # 轻量排版优化
    s = s.replace('<=', '≤').replace('>=', '≥').replace('!=', '≠')
    s = re.sub(r'\s*=\s*', ' = ', s)

    return s


def sympy_to_rich_label(eq_text: str) -> str:
    """兼容旧调用的别名。"""
    return sympy_to_label(eq_text)


def _find_match_paren(text: str, l_idx: int) -> int:
    """找到从左括号开始的匹配右括号位置。"""
    depth = 0
    for i in range(l_idx, len(text)):
        ch = text[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


def estimate_label_width(text: str, char_width: float = 8.0) -> float:
    """粗略估算公式文本渲染后的宽度（像素）。

    用于后端提示前端图例布局，实际渲染由前端决定。
    """
    i = 0
    width = 0.0
    while i < len(text):
        if text[i] == '√' and i + 1 < len(text) and text[i + 1] == '(':
            r_idx = _find_match_paren(text, i + 1)
            if r_idx != -1:
                rad = text[i + 1:r_idx + 1]
                width += max(6.0, char_width * 0.62)
                width += estimate_label_width(rad, char_width)
                i = r_idx + 1
                continue
        width += char_width
        i += 1
    return width
