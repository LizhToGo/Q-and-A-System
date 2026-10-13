from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.app.db import Base, SessionLocal, create_tables
from backend.app import db as app_db
from backend.app.models import Course, Formula, Material, MaterialChunk, QaCase, Topic


COURSE_DATA = {
    "数字信号处理": [
        ("采样定理", "采样频率至少为信号最高频率的两倍，否则会产生混叠。", "fmax = fs / 2", [{"value": 4, "unit": "kHz"}], {"frequency_hz": 1000}),
        ("离散卷积", "离散时间线性时不变系统的输出可以用输入与单位脉冲响应的离散卷积表示。", "y[n] = sum(x[k] * h[n-k])", [], {}),
        ("离散傅里叶变换", "DFT 将有限长离散序列变换到离散频域。", "X[k] = sum(x[n] * exp(-j*2*pi*k*n/N))", [], {}),
        ("Z变换", "Z变换的收敛域与序列的因果性和系统稳定性有关。", "X(z) = sum(x[n] * z**(-n))", [], {}),
        ("FIR滤波器", "FIR 系统没有反馈，容易实现严格线性相位。", "y[n] = sum(h[k] * x[n-k])", [], {}),
        ("IIR滤波器", "IIR 滤波器包含反馈，通常可以用较低阶数满足选择性要求。", "H(z) = Y(z) / X(z)", [], {}),
        ("频谱分析", "频谱描述信号在频率域的分布，幅度谱和相位谱提供不同信息。", "X(exp(jw)) = sum(x[n] * exp(-j*w*n))", [], {}),
    ],
    "信号与系统": [
        ("连续时间信号", "连续时间信号在时间轴上连续定义，常用函数或波形描述。", "x(t) = A*cos(w*t)", [], {}),
        ("离散时间信号", "离散时间信号只在离散时刻取值，单位抽样序列是基本序列。", "x[n] = x(nT)", [], {}),
        ("线性时不变系统", "线性时不变系统同时满足叠加性、齐次性和时不变性。", "y(t) = x(t) * h(t)", [], {}),
        ("卷积积分", "连续时间线性时不变系统的输出等于输入与单位冲激响应的卷积。", "y(t) = integral(x(tau)*h(t-tau))", [], {}),
        ("傅里叶变换", "傅里叶变换把时域信号表示转换为频域表示。", "X(w) = integral(x(t)*exp(-j*w*t))", [], {}),
        ("拉普拉斯变换", "拉普拉斯变换的收敛域与信号和系统的性质相关。", "X(s) = integral(x(t)*exp(-s*t))", [], {}),
        ("系统稳定性", "有界输入产生有界输出是系统稳定性的基本判据。", "integral(abs(h(t))) < infinity", [], {}),
    ],
    "模拟电子技术基础": [
        ("二极管", "二极管具有单向导电特性，正向导通、反向截止是理想模型的基本描述。", "i_D = I_S * (exp(u_D/(n*UT)) - 1)", [], {}),
        ("三极管放大", "三极管可以在放大区实现电流控制和电压放大。", "i_C = beta * i_B", [], {}),
        ("运算放大器", "理想运算放大器在负反馈线性工作时具有虚短和虚断特性。", "u_o = A*(u_p-u_n)", [], {}),
        ("负反馈", "负反馈可以降低增益对参数变化的敏感性并改善线性度。", "Af = A/(1 + A*F)", [], {}),
        ("RC低通滤波", "一阶 RC 低通网络在截止频率以上逐渐衰减高频分量。", "fc = 1/(2*pi*R*C)", [], {}),
        ("频率响应", "频率响应包括幅频特性和相频特性。", "H(jw) = Uo(jw) / Ui(jw)", [], {}),
        ("功率放大", "功率放大级关注输出功率、效率和失真之间的折中。", "eta = Po / Pdc", [], {}),
    ],
    "数字电子技术基础": [
        ("逻辑门", "与、或、非等基本逻辑门构成数字逻辑电路。", "Y = A * B", [], {}),
        ("布尔代数", "布尔代数用于描述和化简数字逻辑关系。", "A + A*B = A", [], {}),
        ("组合逻辑", "组合逻辑电路的输出只取决于当前输入。", "Y = f(X)", [], {}),
        ("时序逻辑", "时序逻辑电路的输出同时取决于当前输入和历史状态。", "Q_next = f(Q, X)", [], {}),
        ("触发器", "触发器是能够存储一位二进制信息的基本时序单元。", "Q_next = D", [], {}),
        ("计数器", "计数器按照时钟脉冲改变状态并实现计数。", "N_states = 2^n", [], {}),
        ("模数转换", "ADC 将连续幅值映射为有限位数的数字编码。", "q = V_range / 2^n", [], {}),
    ],
}

# These are deliberately limited to expressions handled reliably by the formula
# validator.  Summations, transforms, and bracketed relations remain text claims.
SAFE_FORMULA_TOPICS = {
    "采样定理", "IIR滤波器", "连续时间信号", "线性时不变系统", "二极管",
    "三极管放大", "运算放大器", "负反馈", "RC低通滤波", "频率响应",
    "功率放大", "逻辑门", "布尔代数", "触发器", "计数器", "模数转换",
}

NEGATIVE_CLAIMS = {
    "采样定理": "采样频率低于信号最高频率的两倍时也不会产生混叠。",
    "离散卷积": "离散时间系统的输出不需要输入与单位脉冲响应的卷积。",
    "离散傅里叶变换": "DFT只在连续时间域工作，不把有限长离散序列变换到频域。",
    "Z变换": "Z变换的收敛域与序列因果性和系统稳定性无关。",
    "FIR滤波器": "FIR系统包含反馈，并且不能实现严格线性相位。",
    "IIR滤波器": "IIR滤波器没有反馈，只能用很高阶数满足选择性要求。",
    "频谱分析": "频谱只描述时域波形，不提供幅度谱或相位谱信息。",
    "连续时间信号": "连续时间信号只在离散时刻定义，不能用连续时间函数描述。",
    "离散时间信号": "离散时间信号在所有时间点连续取值，不使用单位抽样序列。",
    "线性时不变系统": "线性时不变系统不满足叠加性，也不满足时不变性。",
    "卷积积分": "连续时间系统输出不需要输入与单位冲激响应的卷积积分。",
    "傅里叶变换": "傅里叶变换把频域表示转换成时域表示，而不是时域转换到频域。",
    "拉普拉斯变换": "拉普拉斯变换的收敛域与信号和系统性质没有关系。",
    "系统稳定性": "只要输入有界，输出无界也仍然满足有界输入有界输出稳定性。",
    "二极管": "理想二极管反向导通、正向截止，不具有单向导电特性。",
    "三极管放大": "三极管在放大区不能控制电流，也不能实现电压放大。",
    "运算放大器": "负反馈线性工作时理想运算放大器不具有虚短和虚断特性。",
    "负反馈": "负反馈会增大增益对参数变化的敏感性并恶化线性度。",
    "RC低通滤波": "一阶RC低通网络在截止频率以上会放大高频分量。",
    "频率响应": "频率响应只包括幅频特性，不包括相频特性。",
    "功率放大": "功率放大级只关注电压增益，不需要考虑输出功率、效率和失真。",
    "逻辑门": "与、或、非不是数字逻辑电路的基本逻辑门。",
    "布尔代数": "布尔代数不能用于描述或化简数字逻辑关系。",
    "组合逻辑": "组合逻辑电路的输出取决于历史状态，而与当前输入无关。",
    "时序逻辑": "时序逻辑电路的输出只取决于当前输入，不保存历史状态。",
    "触发器": "触发器不能存储二进制信息，只能产生连续模拟输出。",
    "计数器": "计数器不随时钟脉冲改变状态，也不能实现计数。",
    "模数转换": "ADC把连续幅值映射成无限精度的连续模拟量，而不是有限位数字编码。",
}


def _seed_metadata(expected: list[dict[str, Any]], modality: dict[str, Any], material_kind: str) -> dict[str, Any]:
    return {
        "origin": "synthetic_demo_seed",
        "provenance": "deterministic demonstration content; not extracted from a textbook",
        "review_status": "pending_explicit_review",
        "material_kind": material_kind,
        "expected_values": expected,
        "modality": modality,
    }


def _add_material(db: Session, course: Course, topic: Topic, material_type: str, title: str, content: str, metadata: dict[str, Any], suffix: str, tags: list[str]) -> Material:
    material = Material(
        course_id=course.id,
        topic_id=topic.id,
        material_type=material_type,
        title=title,
        content=content,
        page_number=None,
        section=topic.name,
        source_anchor=f"seed:demo:{course.name}:{topic.name}:{suffix}",
        tags=tags,
        metadata_json=metadata,
        verified=False,
    )
    db.add(material)
    db.flush()
    db.add(MaterialChunk(material_id=material.id, chunk_index=0, text=content, keywords=tags))
    return material
_GENERATE_DEFAULT = object()


def seed_database(reset: bool = False, generate_cases_fn: Callable[[], None] | None | object = _GENERATE_DEFAULT) -> None:
    """Seed only transparent demo material; case generation is injectable for tests."""
    create_tables()
    db = SessionLocal()
    try:
        if reset:
            Base.metadata.drop_all(bind=app_db.engine)
            Base.metadata.create_all(bind=app_db.engine)
        existing = {course.name: course for course in db.query(Course).all()}
        for course_name, topics in COURSE_DATA.items():
            course = existing.get(course_name)
            if not course:
                course = Course(name=course_name, description=f"{course_name}的演示知识库")
                db.add(course)
                db.flush()
            for topic_name, description, formula, expected, modality in topics:
                topic = db.query(Topic).filter_by(course_id=course.id, name=topic_name).first()
                if not topic:
                    topic = Topic(course_id=course.id, name=topic_name, description=description)
                    db.add(topic)
                    db.flush()
                # A reset creates a clean set. On a non-reset run, retain existing
                # user/imported material and avoid duplicating the demo anchors.
                if db.query(Material).filter_by(course_id=course.id, topic_id=topic.id, source_anchor=f"seed:demo:{course_name}:{topic_name}:text").first():
                    continue
                base_metadata = _seed_metadata(expected, modality, "text")
                text_material = _add_material(db, course, topic, "text", topic_name, description, base_metadata, "text", [topic_name])
                if topic_name in SAFE_FORMULA_TOPICS:
                    formula_metadata = _seed_metadata(expected, modality, "formula")
                    formula_material = _add_material(db, course, topic, "formula", f"{topic_name}规范公式", formula, formula_metadata, "formula", [topic_name, "公式"])
                    db.add(Formula(material_id=formula_material.id, raw_formula=formula, canonical_expression=formula, variables_json={}))
                else:
                    # Keep potentially unsafe summation/integral syntax as a
                    # textual relation rather than claiming it is parseable.
                    db.add(MaterialChunk(material_id=text_material.id, chunk_index=1, text=f"文字关系（待人工解析）：{formula}", keywords=[topic_name, "关系"]))
                _add_material(db, course, topic, "example", f"{topic_name}演示例", f"演示例（合成资料）：在讨论{topic_name}时，先识别其定义，再核对相关条件。", _seed_metadata(expected, modality, "example"), "example", [topic_name, "示例"])
                if topic_name == "采样定理":
                    waveform_metadata = _seed_metadata([], modality, "waveform")
                    _add_material(db, course, topic, "waveform", "采样定理演示波形", "合成波形演示：正弦信号主频为 1000 Hz，采样频率为 8 kHz。", waveform_metadata, "waveform", [topic_name, "波形"])
                    spectrum_metadata = _seed_metadata([], modality, "spectrum")
                    _add_material(db, course, topic, "spectrum", "采样定理演示频谱", "合成频谱演示：主频为 1000 Hz；该资料不是教材页图。", spectrum_metadata, "spectrum", [topic_name, "频谱"])
        db.commit()
    finally:
        db.close()
    if generate_cases_fn is _GENERATE_DEFAULT:
        generate_cases()
    elif generate_cases_fn is not None:
        generate_cases_fn()


def _case(case_id: str, course_id: int, topic_id: int, question: str, answer: str, status: str, category: str, error_type: str, evidence_theme: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "course_id": course_id,
        "topic_id": topic_id,
        "question": question,
        "answer": answer,
        "expected_status": status,
        "expected_category": category,
        "category": category,
        "label": "positive" if status != "pass" else "negative",
        "error_type": error_type,
        "expected_evidence_theme": evidence_theme,
    }


WRONG_FORMULAS = {
    "采样定理": "fmax = 2 * fs",
    "IIR滤波器": "H(z) = X(z) / Y(z)",
    "连续时间信号": "x(t) = A*sin(w*t)",
    "线性时不变系统": "y(t) = x(t) + h(t)",
    "二极管": "i_D = I_S * (exp(u_D/(n*UT)) + 1)",
    "三极管放大": "i_C = beta + i_B",
    "运算放大器": "u_o = A*(u_p+u_n)",
    "负反馈": "Af = A/(1 - A*F)",
    "RC低通滤波": "fc = 1/(2*pi*R+C)",
    "频率响应": "H(jw) = Ui(jw) / Uo(jw)",
    "功率放大": "eta = Pdc / Po",
    "逻辑门": "Y = A + B",
    "布尔代数": "A + A*B = B",
    "触发器": "Q_next = D + 1",
    "计数器": "N_states = 2*n",
    "模数转换": "q = V_range * 2^n",
}


def build_evaluation_cases(topics: Sequence[Topic]) -> list[dict[str, Any]]:
    """Build a deterministic, non-duplicated evaluation set from seeded topics."""
    rows: list[dict[str, Any]] = []
    for topic in topics:
        course_id = topic.course_id
        topic_id = topic.id
        prefix = f"course{course_id}-topic{topic_id}"
        formula = next((item[2] for items in COURSE_DATA.values() for item in items if item[0] == topic.name), "")
        safe_formula = topic.name in SAFE_FORMULA_TOPICS
        rows.extend([
            _case(f"{prefix}-definition", course_id, topic_id, f"请定义{topic.name}并说明其核心用途。", topic.description, "pass", "text", "", "definition and scope"),
            _case(f"{prefix}-negation", course_id, topic_id, f"下列关于{topic.name}的说法是否正确？", NEGATIVE_CLAIMS.get(topic.name, f"{topic.name}不具备上述定义中的任何性质。"), "fail", "text", "contradictory_claim", "specific negation or wrong condition"),
            _case(f"{prefix}-formula-correct", course_id, topic_id, f"请给出{topic.name}的规范关系并解释符号含义。" if safe_formula else f"请用文字说明{topic.name}的关系，避免把求和或积分当作已解析公式。", formula if safe_formula else f"{topic.description} 其规范关系为文字性描述：{formula}。", "pass", "formula" if safe_formula else "text", "" if safe_formula else "textual_relation_not_parsed", "supported canonical expression" if safe_formula else "textual relation"),
            _case(f"{prefix}-formula-wrong", course_id, topic_id, f"请检查这条{topic.name}关系是否成立。" if safe_formula else f"请指出该{topic.name}条件陈述的问题。", WRONG_FORMULAS.get(topic.name, formula.replace("=", "≠", 1)) if safe_formula else NEGATIVE_CLAIMS.get(topic.name, f"{topic.name}的输出与定义和条件无关。"), "fail", "formula" if safe_formula else "text", "non_equivalent" if safe_formula else "wrong_condition", "formula mismatch" if safe_formula else "condition contradiction"),
            _case(f"{prefix}-application", course_id, topic_id, f"在实验或分析中如何应用{topic.name}？", f"应用{topic.name}时，应先核对定义、输入条件和适用范围，再依据资料中的核心结论进行判断。", "pass", "text", "", "application and boundary conditions"),
        ])

    sampling = next((topic for topic in topics if topic.name == "采样定理"), None)
    if sampling is not None:
        rows.extend([
            _case("sampling-numeric-4khz", sampling.course_id, sampling.id, "采样频率为8 kHz时，最高可表示频率是多少？", "根据 fmax = fs / 2，最高可表示频率为4 kHz。", "pass", "numeric", "", "Nyquist boundary and unit conversion"),
            _case("sampling-numeric-8khz", sampling.course_id, sampling.id, "采样频率为8 kHz时，最高可表示频率是多少？", "根据 fmax = fs / 2，最高可表示频率为8 kHz。", "fail", "numeric", "wrong_value", "Nyquist boundary and wrong value"),
            _case("sampling-spectrum-1000hz", sampling.course_id, sampling.id, "演示频谱中的主频是多少？", "演示频谱主频为1000 Hz，波形与频谱的频率一致。", "pass", "cross_modal", "", "waveform-spectrum agreement"),
            _case("sampling-spectrum-6000hz", sampling.course_id, sampling.id, "演示频谱中的主频是多少？", "演示频谱主频为6000 Hz，波形与频谱的频率一致。", "fail", "cross_modal", "wrong_value_and_spectrum", "waveform-spectrum mismatch"),
        ])
    pairs = [(row["question"], row["answer"]) for row in rows]
    if len(pairs) != len(set(pairs)):
        raise ValueError("evaluation cases contain duplicate question/answer pairs")
    if len(rows) < 120:
        raise ValueError(f"evaluation dataset unexpectedly contains only {len(rows)} cases")
    return rows


def generate_cases(output: Path | None = None) -> None:
    output = output or Path(__file__).resolve().parent / "evaluation" / "cases.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        topics = list(db.query(Topic).order_by(Topic.course_id, Topic.id))
        rows = build_evaluation_cases(topics)
        output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        # Keep ORM persistence limited to columns represented by QaCase. The
        # richer labels/evidence themes remain in cases.jsonl for evaluation.
        db.execute(delete(QaCase))
        db.add_all([QaCase(**{key: row[key] for key in ("case_id", "course_id", "topic_id", "question", "answer", "expected_status", "expected_category", "error_type")}) for row in rows])
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    seed_database(args.reset)
    print("seeded courses=4 topics=28 cases>=120")
