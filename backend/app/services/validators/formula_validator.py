from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sympy import Rational, Symbol, cos, exp, log, pi, simplify, sin, sqrt, tan

from .types import Evidence, ValidationResult

# These bounds apply before symbolic construction; no Python source is evaluated.
_MAX_LENGTH = 1000
_MAX_TOKENS = 256
_MAX_DEPTH = 24
_MAX_FORMULAS = 40
_BLOCK_RE = re.compile(r"\$\$(.*?)\$\$|\\\[(.*?)\\\]|\\\((.*?)\\\)|(?<!\$)\$(?!\$)(.*?)(?<!\\)\$", re.S)
_TOKEN_RE = re.compile(r"\s+|\\[A-Za-z]+|\\[,;! ]|\d+(?:\.\d+)?|[A-Za-z]+|[α-ωΑ-Ω]|<=|>=|==|[+*/^_=<>(){}−×·≤≥π-]")
_GREEK = frozenset("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron rho sigma tau upsilon phi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split())
_FUNCTIONS = {"sin": sin, "cos": cos, "tan": tan, "exp": exp, "log": log, "ln": log, "sqrt": sqrt}
_COMMANDS = {"cdot": "*", "times": "*", "le": "<=", "leq": "<=", "ge": ">=", "geq": ">=", "lt": "<", "gt": ">", "dfrac": "frac", "tfrac": "frac"}
_UNICODE_GREEK = dict(zip("αβγδεζηθικλμνξορστυφχψω", "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron rho sigma tau upsilon phi chi psi omega".split()))
_UNICODE_GREEK.update(dict(zip("ΓΔΘΛΞΠΣΥΦΨΩ", "Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split())))
_MATH_WORDS = _GREEK | frozenset(_FUNCTIONS) | {"pi", "frac", "fmax", "fmin"}
_PROSE_WORDS = frozenset("an as at be by do for from if in is it of on or so the to was were with".split())


class _ParseLimit(ValueError):
    pass


@dataclass
class _Formula:
    left: Any
    right: Any | None = None
    operator: str | None = None

    @property
    def symbols(self) -> set:
        return self.left.free_symbols | (self.right.free_symbols if self.right is not None else set())

    @property
    def subjects(self) -> set:
        return {value for value in (self.left, self.right) if isinstance(value, Symbol)}

    @property
    def residual(self) -> Any:
        if self.right is None:
            return self.left
        return self.right - self.left if self.operator in {"<", "<="} else self.left - self.right


def _identifier(value: str) -> str:
    return value.replace("_", "").replace("{", "").replace("}", "")


def _tokens(value: str) -> list[str]:
    if len(value) > _MAX_LENGTH:
        raise _ParseLimit("公式超过安全长度限制")
    tokens: list[str] = []
    position = 0
    for match in _TOKEN_RE.finditer(value):
        if match.start() != position:
            raise _ParseLimit("包含暂不支持的记号或语法")
        position = match.end()
        token = match.group()
        if token.isspace() or token in {r"\,", r"\;", r"\!", "\\ "}:
            continue
        if token.startswith("\\"):
            command = token[1:]
            if command in {"left", "right"}:
                continue
            if command not in _GREEK and command not in _FUNCTIONS and command not in {"pi", "frac"} and command not in _COMMANDS:
                raise _ParseLimit("包含暂不支持的 LaTeX 命令（如微积分或排版命令）")
            token = _COMMANDS.get(command, command)
        token = {"−": "-", "×": "*", "·": "*", "≤": "<=", "≥": ">=", "π": "pi", "==": "="}.get(token, _UNICODE_GREEK.get(token, token))
        tokens.append(token)
        if len(tokens) > _MAX_TOKENS:
            raise _ParseLimit("公式超过安全复杂度限制")
    if position != len(value):
        raise _ParseLimit("包含暂不支持的记号或语法")
    return tokens


class _Parser:
    """Bounded arithmetic grammar using only explicit SymPy constructors."""

    def __init__(self, value: str, aliases: dict[str, str] | None = None):
        self.tokens = _tokens(value.strip())
        self.position = 0
        self.depth = 0
        self.aliases = {
            _identifier(source): _identifier(target)
            for source, target in (aliases or {}).items()
            if isinstance(source, str) and isinstance(target, str)
            and len(source) <= 80 and len(target) <= 80
            and re.fullmatch(r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9_{}α-ωΑ-Ω]*", source)
            and re.fullmatch(r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9_{}α-ωΑ-Ω]*", target)
        }

    def peek(self) -> str:
        return self.tokens[self.position] if self.position < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or expected is not None and token != expected:
            raise _ParseLimit("公式不完整或括号不匹配")
        self.position += 1
        return token

    def parse(self) -> _Formula:
        left = self.sum()
        operator = self.peek()
        right = None
        if operator in {"=", "<", ">", "<=", ">="}:
            self.take()
            right = self.sum()
        else:
            operator = None
        if self.peek():
            raise _ParseLimit("暂不支持连锁关系或该公式语法")
        return _Formula(left, right, operator)

    def sum(self) -> Any:
        value = self.product()
        while self.peek() in {"+", "-"}:
            operator = self.take()
            other = self.product()
            value = value + other if operator == "+" else value - other
        return value

    def product(self) -> Any:
        value = self.unary()
        while True:
            token = self.peek()
            if token in {"*", "/"}:
                self.take()
                other = self.unary()
                if token == "/" and other == 0:
                    raise _ParseLimit("分母为零，无法进行等价检查")
                value = value * other if token == "*" else value / other
            elif token and (token in {"(", "{"} or token[0].isalnum()):
                value *= self.unary()
            else:
                return value

    def unary(self) -> Any:
        if self.peek() in {"+", "-"}:
            operator = self.take()
            self.depth += 1
            if self.depth > _MAX_DEPTH:
                raise _ParseLimit("公式超过安全嵌套限制")
            value = self.unary()
            self.depth -= 1
            return -value if operator == "-" else value
        return self.power()

    def power(self) -> Any:
        value = self.atom()
        if self.peek() == "^" or self.peek() == "*" and self.tokens[self.position:self.position + 2] == ["*", "*"]:
            operator = self.take()
            if operator == "*":
                self.take("*")
            exponent = self.unary()
            # Avoid enormous integer powers and symbolic power towers.
            if exponent.is_number:
                if not exponent.is_Rational or abs(exponent) > 12 or exponent.q > 12:
                    raise _ParseLimit("指数超过安全范围")
            elif not isinstance(exponent, Symbol):
                raise _ParseLimit("暂不支持复合符号指数")
            value = value ** exponent
        return value

    def group(self) -> Any:
        opener = self.take()
        if opener not in {"(", "{"}:
            raise _ParseLimit("该记号需要括号参数")
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise _ParseLimit("公式超过安全嵌套限制")
        value = self.sum()
        self.take(")" if opener == "(" else "}")
        self.depth -= 1
        return value

    def atom(self) -> Any:
        token = self.peek()
        if token in {"(", "{"}:
            return self.group()
        token = self.take()
        if token[0].isdigit():
            if len(token) > 24:
                raise _ParseLimit("数值超过安全位数限制")
            return Rational(token)
        if token == "frac":
            numerator = self.group()
            denominator = self.group()
            if denominator == 0:
                raise _ParseLimit("分母为零，无法进行等价检查")
            return numerator / denominator
        if token in _FUNCTIONS:
            self.depth += 1
            if self.depth > _MAX_DEPTH:
                raise _ParseLimit("公式超过安全嵌套限制")
            argument = self.group() if self.peek() in {"(", "{"} else self.unary()
            self.depth -= 1
            return _FUNCTIONS[token](argument)
        if token == "pi":
            return pi
        if not re.fullmatch(r"[A-Za-zα-ωΑ-Ω]+", token):
            raise _ParseLimit("公式不完整或包含不支持的记号")
        if self.peek() == "_":
            self.take()
            braced = self.peek() == "{"
            if braced:
                self.take()
            subscript = self.take()
            if not re.fullmatch(r"[A-Za-z0-9α-ωΑ-Ω]+", subscript):
                raise _ParseLimit("暂不支持该下标形式")
            if braced:
                self.take("}")
            token += subscript
        if len(token) > 40:
            raise _ParseLimit("变量名超过安全长度限制")
        if token[0].islower() and self.peek() == "(":
            raise _ParseLimit("暂不支持未定义的符号函数")
        if token in self.aliases:
            return Symbol(self.aliases[token])
        # Conventional uppercase juxtaposition RC denotes R*C, not a new name.
        if len(token) > 1 and token.isascii() and token.isupper() and token not in _GREEK:
            value = Rational(1)
            for name in token:
                value *= Symbol(self.aliases.get(name, name))
            return value
        return Symbol(token)


def _expression(value: str, aliases: dict[str, str] | None = None) -> _Formula:
    return _Parser(value, aliases).parse()


def _bare_formulas(value: str) -> list[str]:
    # Break prose into runs before matching a relation. Delimited math remains intact.
    value = re.sub(r"\b[A-Za-z]+\b", lambda match: match.group() if match.group() not in _PROSE_WORDS and (len(match.group()) <= 2 or match.group() in _MATH_WORDS or match.start() > 0 and value[match.start() - 1] in {"\\", "_"}) else "\n", value)
    runs = re.findall(r"[A-Za-z0-9α-ωΑ-Ωπ\\_{}()\[\]+*/^=<>≤≥. \t−×·-]+", value)
    result = []
    for run in runs:
        candidate = run.strip(" .\t")
        if re.search(r"[=<>≤≥]|\\(?:leq?|geq?|lt|gt)\b", candidate):
            candidate = candidate.strip("[] ")
            if candidate:
                result.append(candidate)
    return result


def extract_formulas(answer: str) -> list[str]:
    answer = answer[:100_000]
    # Never treat fenced examples/code or fragments of delimited math as bare math.
    answer = re.sub(r"```.*?(?:```|\Z)", lambda match: " " * len(match.group()), answer, flags=re.S)
    found: list[tuple[int, str]] = []
    masked = list(answer)
    for match in _BLOCK_RE.finditer(answer):
        formula = next((group for group in match.groups() if group is not None), "").strip().rstrip("。；，,;.").strip()
        if formula and not re.fullmatch(r"(?:\\?[A-Za-zα-ωΑ-Ω]+(?:_\{?[A-Za-z0-9]+\}?)?|\d+(?:\.\d+)?)", formula):
            found.append((match.start(), formula))
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    remainder = "".join(masked)
    # Unclosed delimiters are limitations, not valid truncated relations.
    remainder = re.sub(r"(?:\$|\\[\[(]).*?(?:\n|\Z)", "\n", remainder)
    for match in re.finditer(r"[^\n]+", remainder):
        for formula in _bare_formulas(match.group()):
            found.append((match.start(), formula))
    ordered = dict.fromkeys(formula for _, formula in sorted(found, key=lambda item: item[0]))
    return list(ordered)[:_MAX_FORMULAS + 1]


def _reference_formulas(evidence: Evidence) -> list[tuple[str, Any]]:
    if not evidence.verified and evidence.source_anchor.startswith("doc:"):
        return []
    if evidence.formulae:
        return [(candidate.strip(), None) for candidate in evidence.formulae if isinstance(candidate, str) and candidate.strip()][:_MAX_FORMULAS]
    reviewed = (evidence.metadata or {}).get("reviewed_formulas", [])
    if evidence.verified and isinstance(reviewed, list):
        candidates = [
            (item["source"].strip(), item.get("conditions"))
            for item in reviewed if isinstance(item, dict) and isinstance(item.get("source"), str) and item["source"].strip()
        ]
        if candidates:
            return candidates[:_MAX_FORMULAS]
    return [(candidate, None) for candidate in extract_formulas(evidence.text)] if evidence.material_type == "formula" else []


def _comparable(answer: _Formula, reference: _Formula) -> bool:
    if (answer.operator is None) != (reference.operator is None):
        return False
    if (answer.operator == "=") != (reference.operator == "="):
        return False
    if answer.symbols == reference.symbols:
        return True
    shared_subjects = answer.subjects & reference.subjects
    if not shared_subjects:
        return False
    answer_support = answer.symbols - shared_subjects
    reference_support = reference.symbols - shared_subjects
    return answer_support <= reference_support or reference_support <= answer_support


def _comparison(answer: _Formula, reference: _Formula) -> tuple[str, str]:
    left, right = answer.residual, reference.residual
    if answer.operator is None:
        equivalent = simplify(left - right) == 0
        return ("pass", "表达式符号等价") if equivalent else ("fail", "同一变量表达式与审核公式不等价")
    if left == 0 or right == 0:
        if answer.operator == "=":
            return ("pass", "等式符号等价") if left == right else ("fail", "等式与审核公式不等价")
        return "unknown", "退化不等式无法可靠比较"
    ratio = simplify(left / right)
    if answer.operator == "=":
        if not ratio.free_symbols and ratio.is_finite is True and ratio.is_zero is False:
            return "pass", "等式在非零常数缩放后等价"
        return "fail", "同一关系与审核公式不等价"
    if ratio.free_symbols:
        symbols = answer.symbols | reference.symbols
        left_poly, right_poly = left.as_poly(*symbols), right.as_poly(*symbols)
        if left_poly is not None and right_poly is not None and left_poly.total_degree() <= 1 and right_poly.total_degree() <= 1:
            return "fail", "不等式边界与审核公式不同"
        return "unknown", "不等式缩放因子的符号无法确定"
    if ratio.is_real is not True:
        return "unknown", "不等式缩放因子的符号无法确定"
    strict_answer = answer.operator in {"<", ">"}
    strict_reference = reference.operator in {"<", ">"}
    if ratio.is_positive is True and strict_answer == strict_reference:
        return "pass", "不等式方向和严格性一致"
    if ratio.is_negative is True:
        return "fail", "不等式方向与审核公式相反"
    if ratio.is_positive is True:
        return "fail", "不等式严格性与审核公式不同"
    return "unknown", "不等式无法可靠比较"


def validate_formula(question: str, answer: str, evidence: list[Evidence]) -> ValidationResult:
    answer_formulas = extract_formulas(answer)
    reference_items = [item for item in evidence if item.formulae or item.material_type == "formula" or (item.metadata or {}).get("reviewed_formulas")]
    evidence_ids = list(dict.fromkeys(item.material_id for item in reference_items))[:3]
    if not answer_formulas:
        if not reference_items:
            return ValidationResult("formula", "not_applicable", "当前回答没有需要校验的公式")
        return ValidationResult("formula", "unknown", "未提取到完整公式，无法进行等价检查", evidence_ids=evidence_ids)
    references = []
    reference_limits = []
    for item in reference_items:
        aliases = (item.metadata or {}).get("variable_aliases", {})
        aliases = aliases if isinstance(aliases, dict) else {}
        for source, conditions in _reference_formulas(item):
            try:
                parsed = _expression(source, aliases)
                references.append((item, source, conditions, aliases, parsed))
            except (ValueError, TypeError, ArithmeticError, RecursionError):
                reference_limits.append({"material_id": item.material_id, "source": source, "conditions": conditions, "reason": "审核公式包含暂不支持的语法或超过安全限制"})
    comparisons = []
    for formula in answer_formulas[:_MAX_FORMULAS]:
        detail: dict[str, Any] = {"formula": formula, "status": "unknown", "reason": "没有同一关系的可信参考公式", "references": []}
        try:
            _expression(formula)
        except _ParseLimit as exc:
            detail["reason"] = str(exc)
            comparisons.append(detail)
            continue
        except (ValueError, TypeError, ArithmeticError, RecursionError):
            detail["reason"] = "该公式暂时无法安全解析"
            comparisons.append(detail)
            continue
        for item, source, conditions, aliases, parsed_reference in references:
            try:
                parsed_answer = _expression(formula, aliases)
                if not _comparable(parsed_answer, parsed_reference):
                    continue
                status, reason = _comparison(parsed_answer, parsed_reference)
            except (ValueError, TypeError, ArithmeticError, RecursionError):
                status, reason = "unknown", "该关系暂时无法可靠进行符号比较"
            detail["references"].append({"material_id": item.material_id, "source": source, "conditions": conditions, "status": status, "reason": reason})
        outcomes = {comparison["status"] for comparison in detail["references"]}
        if "pass" in outcomes and "fail" in outcomes:
            detail.update(status="unknown", reason="同一关系的审核参考存在差异，无法确定")
        elif "pass" in outcomes:
            detail.update(status="pass", reason="与同一关系的审核公式等价")
        elif "fail" in outcomes and "unknown" not in outcomes:
            detail.update(status="fail", reason="与同一关系的审核公式存在矛盾")
        elif detail["references"]:
            detail["reason"] = "同一关系暂时无法可靠比较"
        comparisons.append(detail)
    if len(answer_formulas) > _MAX_FORMULAS or len(answer) > 100_000:
        comparisons.append({"formula": "", "status": "unknown", "reason": "回答超过安全检查数量或长度限制", "references": []})
    checked = sum(item["status"] == "pass" for item in comparisons)
    failed = sum(item["status"] == "fail" for item in comparisons)
    limited = sum(item["status"] == "unknown" for item in comparisons)
    if failed:
        status, message = "fail", f"发现 {failed} 处公式与审核依据矛盾"
        if limited:
            message += f"；另有 {limited} 处暂无法核验"
    elif limited:
        status, message = "unknown", f"有 {limited} 处公式缺少同一关系依据或超出解析范围"
    else:
        status, message = "pass", "回答公式通过同一关系的符号等价检查"
    return ValidationResult(
        "formula", status, message, claim="；".join(answer_formulas), evidence_ids=evidence_ids,
        details={"answer_formulas": answer_formulas, "checked": checked, "failed": failed, "limited": limited, "comparisons": comparisons, "reference_limitations": reference_limits},
    )
