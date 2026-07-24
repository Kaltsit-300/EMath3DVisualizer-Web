"""增强型方程解析器。

从 EMath3DVisualizer 提取并整理，提供更完整的输入标准化：
- Unicode 数学符号转换（√、²、³、π 等）
- 隐式乘法补全
- 关系运算符切分
- 自动识别缺失参数
"""
import re
from typing import Callable, Optional

import sympy as sp


XYZ_COORDS = (sp.Symbol("x"), sp.Symbol("y"), sp.Symbol("z"))
XYZ_SET = set(XYZ_COORDS)


def normalize_equation_text(eq_text: str) -> str:
    """将用户输入的方程文本标准化为 SymPy 可解析的形式。"""
    text = eq_text.strip()

    # Unicode 数学符号转换
    text = text.replace("√", "sqrt")  # 平方根
    text = text.replace("²", "**2")   # 平方
    text = text.replace("³", "**3")   # 立方
    text = text.replace("π", "pi")    # 圆周率

    # 绝对值符号 |...| -> abs(...)
    text = re.sub(r'\|([^|]+?)\|', r'abs(\1)', text)

    # ln 在 SymPy 中对应 log
    text = text.replace("ln(", "log(")

    # 保护含坐标字母的函数名 exp：隐式乘法补全会把 "exp" 中的 e-x、x-p
    # 误判为隐式乘法而拆成 e*x*p。用单个 NUL 占位符替换，处理完后再还原。
    text = text.replace("exp", "\x00")

    # 函数名后加空格，避免 "sin(" 被误判为 sin*(...) 而崩溃
    funcs = [
        "sin", "cos", "tan", "sqrt", "log", "abs",
        "asin", "acos", "atan", "sinh", "cosh", "tanh", "floor", "ceil",
    ]
    for f_name in funcs:
        text = text.replace(f"{f_name}(", f"{f_name} (")

    # 隐式乘法补全：2x -> 2*x, xy -> x*y, x( -> x*(, )( -> )*(
    text = re.sub(r"(\d)([a-zA-Z])", r"\1*\2", text)
    text = re.sub(r"([a-zA-Z])([xyz])", r"\1*\2", text, flags=re.IGNORECASE)
    text = re.sub(r"([xyz])([a-zA-Z])", r"\1*\2", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[a-zA-Z0-9])(?=\()", "*", text)
    text = re.sub(r"(?<=\))(?=[a-zA-Z0-9])", "*", text)

    # 数字 / 字母 与占位符之间的隐式乘法（lambda 避免 \x 转义问题）
    text = re.sub(r"(\d)\x00", lambda m: m.group(1) + "*" + "\x00", text)
    text = re.sub(r"([a-zA-Z])\x00", lambda m: m.group(1) + "*" + "\x00", text)

    # 还原函数名
    text = text.replace("\x00", "exp")

    # 将 ^ 转换为 **
    text = text.replace("^", "**")
    return text


def build_expression(eq_text: str) -> tuple[sp.Expr, str, Optional[str]]:
    """解析方程文本，返回 (表达式, 标准化文本, 关系运算符)。

    表达式为 lhs - rhs 形式，因此 f(x,y,z)=0 的等值面可直接绘制。
    """
    normalized = normalize_equation_text(eq_text)

    ops = ["==", "!=", ">=", "<=", ">", "<", "="]
    found_op = None
    for op in ops:
        if op in normalized:
            found_op = op
            break

    if found_op:
        lhs, rhs = normalized.split(found_op, 1)
        expr = sp.sympify(f"({lhs})-({rhs})")
    else:
        expr = sp.sympify(normalized)

    return expr, normalized, found_op


def parse_equation(
    eq_text: str,
    params: Optional[dict] = None,
    add_param_callback: Optional[Callable[[str, float], None]] = None,
) -> Optional[dict]:
    """解析方程并返回绘制所需字典。

    Args:
        eq_text: 用户输入的方程文本，例如 "x**2 + y**2 + z**2 = 10"
        params: 当前已定义的参数字典 {name: value}
        add_param_callback: 发现新参数时的回调，签名为 (name, default_value)

    Returns:
        解析结果字典，包含 expr/syms/sym_names/dims/raw，解析失败返回 None。
    """
    params = params or {}
    expr, normalized, _ = build_expression(eq_text)

    all_syms = expr.free_symbols
    params_needed = sorted([str(s) for s in (all_syms - XYZ_SET)])

    for p_name in params_needed:
        if p_name not in params and add_param_callback:
            add_param_callback(p_name, 1.0)

    syms = sorted(list(all_syms & XYZ_SET), key=str)
    if not syms and not params_needed:
        return None

    return {
        "expr": expr,
        "syms": syms,
        "sym_names": [str(s) for s in syms],
        "dims": len(syms),
        "raw": normalized,
        "params_needed": params_needed,
    }


def parse_for_api(eq_text: str, params: Optional[dict] = None) -> tuple[Optional[dict], list[str]]:
    """API 场景解析：返回 parsed 结构与缺失参数列表（不做 UI 回调）。"""
    params = params or {}
    expr, normalized, _ = build_expression(eq_text)

    all_syms = expr.free_symbols
    params_needed = sorted([str(s) for s in (all_syms - XYZ_SET)])
    missing_params = [name for name in params_needed if name not in params]

    syms = sorted(list(all_syms & XYZ_SET), key=str)
    if not syms and not params_needed:
        return None, []

    parsed = {
        "expr": expr,
        "syms": syms,
        "sym_names": [str(s) for s in syms],
        "dims": len(syms),
        "raw": normalized,
        "params_needed": params_needed,
    }
    return parsed, missing_params
