"""Fixed numerical operations. Executed by local_tools in an isolated subprocess."""
from __future__ import annotations

import ast
import json
import math
import sys
from pathlib import Path

import numpy as np

MAX_SAMPLES = 4096
MAX_SEQUENCE = 256


class ToolInputError(ValueError):
    pass


def number(value, name, *, minimum=-1e6, maximum=1e6):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(f"{name} must be a finite number")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise ToolInputError(f"{name} must be in [{minimum}, {maximum}]")
    return float(value)


def integer(value, name, minimum, maximum):
    result = number(value, name, minimum=minimum, maximum=maximum)
    if result != int(result):
        raise ToolInputError(f"{name} must be an integer")
    return int(result)


def fields(arguments, allowed, required=()):
    if not isinstance(arguments, dict):
        raise ToolInputError("arguments must be an object")
    unknown = set(arguments) - set(allowed)
    missing = set(required) - set(arguments)
    if unknown or missing:
        raise ToolInputError(f"Unknown fields: {sorted(unknown)}; missing fields: {sorted(missing)}")


def sequence(value, name, limit=MAX_SEQUENCE):
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise ToolInputError(f"{name} must contain 1..{limit} real numbers")
    return np.asarray([number(item, name) for item in value], dtype=float)


def expression_values(expression, grid, variable="t"):
    """Interpret an allowlisted AST; never compile/eval model text."""
    grid = np.asarray(grid, dtype=float)
    if not isinstance(expression, str) or not 1 <= len(expression) <= 512:
        raise ToolInputError("expression must contain 1..512 characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ToolInputError("Invalid expression syntax") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ToolInputError("Expression is too complex")
    functions = {
        "sin": np.sin, "cos": np.cos, "tan": np.tan, "exp": np.exp,
        "sqrt": np.sqrt, "log": np.log, "abs": np.abs, "sinc": np.sinc,
        "sign": np.sign,
        "rect": lambda x: np.where(np.abs(x) < .5, 1., np.where(np.abs(x) == .5, .5, 0.)),
        "step": lambda x: np.where(x > 0, 1., np.where(x == 0, .5, 0.)),
    }

    def literal(node):
        if isinstance(node, ast.Constant):
            return number(node.value, "literal", minimum=-32, maximum=32)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = literal(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        raise ToolInputError("Power exponent must be a literal in [-32, 32]")

    def visit(node, depth=0):
        if depth > 24:
            raise ToolInputError("Expression nesting is too deep")
        if isinstance(node, ast.Constant):
            return number(node.value, "literal")
        if isinstance(node, ast.Name):
            if node.id == variable:
                return grid
            if node.id in {"pi", "e"}:
                return math.pi if node.id == "pi" else math.e
            raise ToolInputError(f"Only {variable}, pi and e are allowed names")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left = visit(node.left, depth + 1)
            if isinstance(node.op, ast.Pow):
                return np.power(left, literal(node.right))
            right = visit(node.right, depth + 1)
            operations = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide}
            operation = operations.get(type(node.op))
            if operation is not None:
                return operation(left, right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in functions and len(node.args) == 1 and not node.keywords:
                return functions[node.func.id](visit(node.args[0], depth + 1))
        raise ToolInputError("Unsupported expression: attributes, indexing, comparisons and arbitrary calls are forbidden")

    try:
        with np.errstate(all="raise", under="ignore"):
            values = np.asarray(visit(tree.body), dtype=float)
            values = np.broadcast_to(values, grid.shape)
    except (FloatingPointError, OverflowError, TypeError, ValueError) as exc:
        if isinstance(exc, ToolInputError):
            raise
        raise ToolInputError(f"Expression is undefined or nonfinite on the requested grid: {exc}") from exc
    if not np.isfinite(values).all():
        raise ToolInputError("Expression produced nonfinite values")
    return values


def grid_parameters(args, *, defaults=(-10., 10.), endpoint=True):
    start = number(args.get("start", defaults[0]), "start")
    stop = number(args.get("stop", defaults[1]), "stop")
    if not start < stop or stop - start > 1e6:
        raise ToolInputError("Require start < stop and window length <= 1000000")
    count = integer(args.get("samples", 1024), "samples", 16, MAX_SAMPLES)
    return np.linspace(start, stop, count, endpoint=endpoint)


def complex_data(values):
    values = np.asarray(values)
    return {"real": values.real.tolist(), "imag": values.imag.tolist()}


def waveform(args):
    fields(args, {"expression", "domain", "start", "stop", "samples"}, {"expression", "domain", "start", "stop"})
    domain = args["domain"]
    if domain == "discrete":
        if "samples" in args:
            raise ToolInputError("Discrete waveform uses every integer index; omit samples")
        start = integer(args["start"], "start", -1000000, 1000000)
        stop = integer(args["stop"], "stop", -1000000, 1000000)
        if not 0 <= stop - start < MAX_SAMPLES:
            raise ToolInputError("Discrete waveform requires start <= stop and at most 4096 indices")
        grid = np.arange(start, stop + 1)
        values = expression_values(args["expression"], grid, "n")
    elif domain == "continuous":
        grid = grid_parameters(args)
        values = expression_values(args["expression"], grid)
    else:
        raise ToolInputError("domain must be continuous or discrete")
    return {
        "title": "函数波形", "summary": f"{args['expression']} 的{'连续时间' if domain == 'continuous' else '离散时间'}采样波形",
        "data": {"coordinates": grid.tolist(), "values": values.tolist(), "domain": domain},
        "notes": ["连续曲线由采样值连线，不是符号推导证明。", "sinc(x)=sin(pi*x)/(pi*x)；矩形函数在边界取半高，阶跃函数 step(0)=1/2。"],
        "plot": ("waveform", grid, values, domain),
    }


def _fourier_pair(result, time, values):
    result["data"]["time"] = time.tolist()
    result["data"]["time_values"] = values.tolist()
    result["plot"] = ("fourier_pair", time, values, result["plot"])
    result["notes"].append("时域波形与频谱使用同一函数和参数；时域图仅展示有限采样窗口，不代表完整无限时间域。")
    return result


def fourier(args):
    mode = args.get("mode")
    common = {"mode", "samples"}
    permitted = {
        "gaussian": {"amplitude", "sigma", "center", "omega_min", "omega_max"},
        "rectangular": {"amplitude", "width", "center", "omega_min", "omega_max"},
        "constant": {"amplitude", "omega_min", "omega_max"},
        "sinusoid": {"amplitude", "omega0", "phase", "kind", "omega_min", "omega_max"},
        "finite_window": {"expression", "start", "stop"},
    }
    if mode not in permitted:
        raise ToolInputError("Unsupported Fourier mode; use an ideal supported family or finite_window")
    fields(args, common | permitted[mode], {"mode", "expression", "start", "stop"} if mode == "finite_window" else {"mode"})
    notes = ["采用角频率约定：X(omega)=积分 x(t) exp(-j*omega*t) dt，逆变换系数为 1/(2*pi)。"]
    if mode == "finite_window":
        t = grid_parameters(args, endpoint=False)
        values = expression_values(args["expression"], t)
        dt = float(t[1] - t[0])
        omega = np.fft.fftshift(np.fft.fftfreq(len(t), dt)) * 2 * math.pi
        spectrum = np.fft.fftshift(np.fft.fft(values)) * dt * np.exp(-1j * omega * t[0])
        data = {"omega": omega.tolist(), "spectrum": complex_data(spectrum), "dt": dt, "window": [args["start"], args["stop"]], "method": "finite_window_fft"}
        notes += ["有限窗矩形积分/FFT近似，不是无限时间域的精确变换。窗口为 [start,stop)，频谱泄漏、截断和混叠受窗口与采样间隔影响。", f"频率格点间隔为 {2 * math.pi / (len(t) * dt):.8g} rad/time，奈奎斯特角频率为 {math.pi / dt:.8g}。"]
        return _fourier_pair({"title": "有限窗傅里叶变换对照", "summary": "采样信号经矩形窗截断后的数值频谱，不等同于理想变换", "data": data, "notes": notes, "plot": ("spectrum", omega, spectrum, "Finite-window FFT approximation")}, t, values)
    count = integer(args.get("samples", 1024), "samples", 16, MAX_SAMPLES)
    low = number(args.get("omega_min", -20.), "omega_min")
    high = number(args.get("omega_max", 20.), "omega_max")
    if low >= high:
        raise ToolInputError("omega_min must be less than omega_max")
    omega = np.linspace(low, high, count)
    amplitude = number(args.get("amplitude", 1.), "amplitude")
    center = number(args.get("center", 0.), "center") if mode in {"gaussian", "rectangular"} else 0.
    if mode == "gaussian":
        sigma = number(args.get("sigma", 1.), "sigma", minimum=1e-6)
        spectrum = amplitude * sigma * math.sqrt(2 * math.pi) * np.exp(-.5 * (sigma * omega) ** 2) * np.exp(-1j * omega * center)
        time = np.linspace(center - 4 * sigma, center + 4 * sigma, count)
        time_values = amplitude * np.exp(-.5 * ((time - center) / sigma) ** 2)
        notes += ["精确函数族：x(t)=amplitude*exp(-(t-center)^2/(2*sigma^2))，sigma>0。"]
    elif mode == "rectangular":
        width = number(args.get("width", 1.), "width", minimum=1e-6)
        spectrum = amplitude * width * np.sinc(omega * width / (2 * math.pi)) * np.exp(-1j * omega * center)
        time = np.linspace(center - 2 * width, center + 2 * width, count)
        time_values = amplitude * np.where(np.abs((time - center) / width) < .5, 1., np.where(np.abs((time - center) / width) == .5, .5, 0.))
        notes += ["精确函数族：x(t)=amplitude*rect((t-center)/width)，width>0；两个跳变点的取值不影响连续傅里叶变换。"]
    else:
        impulses = []
        if mode == "constant":
            impulses = [{"omega": 0., "weight_real": 2 * math.pi * amplitude, "weight_imag": 0.}]
            time = np.linspace(-5., 5., count)
            time_values = np.full(count, amplitude)
        else:
            omega0 = number(args.get("omega0", 1.), "omega0", minimum=0.)
            phase = number(args.get("phase", 0.), "phase")
            kind = args.get("kind", "cos")
            if kind not in {"cos", "sin"}:
                raise ToolInputError("kind must be cos or sin")
            span = min(2 * math.pi / omega0, 5e5) if omega0 else 5.
            time = np.linspace(-span, span, count)
            time_values = amplitude * (np.cos(omega0 * time + phase) if kind == "cos" else np.sin(omega0 * time + phase))
            positive = math.pi * amplitude * np.exp(1j * phase) * (1 if kind == "cos" else -1j)
            negative = np.conjugate(positive)
            if omega0 == 0:
                impulses = [{"omega": 0., "weight_real": float((positive + negative).real), "weight_imag": 0.}]
            else:
                impulses = [{"omega": -omega0, "weight_real": float(negative.real), "weight_imag": float(negative.imag)}, {"omega": omega0, "weight_real": float(positive.real), "weight_imag": float(positive.imag)}]
        notes += ["精确广义函数变换：箭头表示狄拉克冲激，标注值是复数冲激权重（面积），不是有限高度或FFT频率格点值。", "常量频谱为 2*pi*A*delta(omega)；正弦族为 A*cos(omega0*t+phase) 或 A*sin(omega0*t+phase)。"]
        if any(not low <= item["omega"] <= high for item in impulses):
            notes += ["部分冲激位于所选显示范围外，数值数据仍保留全部冲激。"]
        return _fourier_pair({"title": "理想冲激频谱与时域对照", "summary": "精确广义函数频谱，图中展示冲激权重而非普通频谱幅值", "data": {"method": "ideal_distribution", "impulses": impulses, "omega_range": [low, high]}, "notes": notes, "plot": ("impulses", impulses, low, high)}, time, time_values)
    return _fourier_pair({"title": "解析傅里叶变换对照", "summary": f"{'高斯函数' if mode == 'gaussian' else '矩形函数'}的解析变换，以采样点绘图", "data": {"omega": omega.tolist(), "spectrum": complex_data(spectrum), "method": "analytic"}, "notes": notes, "plot": ("spectrum", omega, spectrum, "Exact analytic transform (sampled display)")}, time, time_values)


def frequency_response(args):
    fields(args, {"domain", "numerator", "denominator", "omega_min", "omega_max", "samples"}, {"domain", "numerator", "denominator"})
    b = sequence(args["numerator"], "numerator", 17)
    a = sequence(args["denominator"], "denominator", 17)
    if not np.any(a):
        raise ToolInputError("Denominator cannot be the zero polynomial")
    if not np.any(b):
        raise ToolInputError("Zero transfer function has undefined phase and no isolated zeros; use a nonzero numerator")
    domain = args["domain"]
    count = integer(args.get("samples", 1024), "samples", 16, MAX_SAMPLES)
    notes = ["系数为实数。零极点表示未经约分的有理函数，公共因子不自动消去；仅凭系数不推断收敛域或因果性。"]
    if domain == "analog":
        b = np.trim_zeros(b, "f")
        a = np.trim_zeros(a, "f")
        low = number(args.get("omega_min", .01), "omega_min", minimum=1e-8)
        high = number(args.get("omega_max", 100.), "omega_max", minimum=1e-8)
        if low >= high:
            raise ToolInputError("Analog frequency interval must satisfy 0 < omega_min < omega_max")
        omega = np.geomspace(low, high, count)
        numerator = np.polyval(b, 1j * omega)
        denominator = np.polyval(a, 1j * omega)
        pole_coefficients, zero_coefficients = a, b
        notes += ["模拟系统系数按 s 的降幂排列，计算 H(j*omega)，角频率单位为 rad/time，使用对数频率网格。"]
    elif domain == "digital":
        b = np.trim_zeros(b, "b")
        a = np.trim_zeros(a, "b")
        if a[0] == 0:
            raise ToolInputError("Digital denominator[0] must be nonzero (causal difference-equation convention)")
        low = number(args.get("omega_min", 0.), "omega_min", minimum=0., maximum=math.pi)
        high = number(args.get("omega_max", math.pi), "omega_max", minimum=0., maximum=math.pi)
        if low >= high:
            raise ToolInputError("Digital interval must satisfy 0 <= omega_min < omega_max <= pi")
        omega = np.linspace(low, high, count)
        q = np.exp(-1j * omega)
        numerator = np.polynomial.polynomial.polyval(q, b)
        denominator = np.polynomial.polynomial.polyval(q, a)
        degree = max(len(a), len(b)) - 1
        pole_coefficients = np.pad(a, (0, degree + 1 - len(a)))
        zero_coefficients = np.pad(b, (0, degree + 1 - len(b)))
        notes += ["数字系统系数按 z^-1 的升幂排列，计算 H(exp(j*omega))，角频率单位为 rad/sample。分子分母同乘 z 的相同次幂后求有限零极点，包含原点处的延时极点。"]
    else:
        raise ToolInputError("domain must be analog or digital")
    numerator_scale = np.polyval(np.abs(b), omega) if domain == "analog" else np.sum(np.abs(b))
    denominator_scale = np.polyval(np.abs(a), omega) if domain == "analog" else np.sum(np.abs(a))
    roundoff_factor = 32 * np.finfo(float).eps
    if np.any(np.abs(denominator) <= roundoff_factor * denominator_scale):
        raise ToolInputError("Frequency interval samples a pole or numerically singular denominator; choose a nonsingular interval")
    response = numerator / denominator
    unresolved_zero = np.abs(numerator) <= roundoff_factor * numerator_scale
    response[unresolved_zero] = 0
    if not np.isfinite(response).all():
        raise ToolInputError("Nonfinite frequency response")
    magnitude = np.abs(response)
    nonzero = magnitude > 0
    db = np.full(len(omega), np.nan)
    db[nonzero] = 20 * np.log10(magnitude[nonzero])
    phase = np.full(len(omega), np.nan)
    # Do not unwrap across zeros, where the phase is undefined.
    edges = np.flatnonzero(np.diff(np.r_[False, nonzero, False]))
    for begin, end in zip(edges[::2], edges[1::2]):
        phase[begin:end] = np.unwrap(np.angle(response[begin:end])) * 180 / math.pi
    poles = np.roots(pole_coefficients)
    zeros = np.roots(zero_coefficients)
    if not np.isfinite(poles).all() or not np.isfinite(zeros).all():
        raise ToolInputError("Pole/zero calculation is numerically nonfinite")
    notes += ["响应零点处 magnitude_db 和 phase_degrees 返回 null：幅值为负无穷dB，相位未定义。分子绝对值不超过其多项式绝对值估算上界的32倍机器精度时，按数值上无法分辨的零点处理。"]
    nullable = lambda values: [float(v) if np.isfinite(v) else None for v in values]
    data = {"domain": domain, "omega": omega.tolist(), "response": complex_data(response), "magnitude": magnitude.tolist(), "magnitude_db": nullable(db), "phase_degrees": nullable(phase), "poles": complex_data(poles), "zeros": complex_data(zeros)}
    return {"title": "有理系统频率响应", "summary": f"{'模拟' if domain == 'analog' else '数字'}系统的幅频、展开相位及有限零极点", "data": data, "notes": notes, "plot": ("response", omega, db, phase, poles, zeros, domain)}


def convolution(args):
    fields(args, {"x", "h", "x_start", "h_start"}, {"x", "h"})
    x = sequence(args["x"], "x")
    h = sequence(args["h"], "h")
    x_start = integer(args.get("x_start", 0), "x_start", -1000000, 1000000)
    h_start = integer(args.get("h_start", 0), "h_start", -1000000, 1000000)
    values = np.convolve(x, h, mode="full")
    indices = np.arange(x_start + h_start, x_start + h_start + len(values))
    return {"title": "带索引的离散卷积", "summary": f"完整线性卷积，共 {len(values)} 个输出采样点，起点 n={indices[0]}", "data": {"indices": indices.tolist(), "values": values.tolist(), "x_indices": list(range(x_start, x_start + len(x))), "h_indices": list(range(h_start, h_start + len(h))), "x": x.tolist(), "h": h.tolist()}, "notes": ["y[n]=sum_k x[k]*h[n-k]；输入数组表示有限长序列，所给索引范围外为零。结果是完整线性卷积，不是循环卷积。"], "plot": ("convolution", indices, values, x, h, x_start, h_start)}


def compute(tool, arguments):
    operations = {"waveform": waveform, "fourier": fourier, "frequency_response": frequency_response, "convolution": convolution}
    if tool not in operations:
        raise ToolInputError("Unknown tool")
    with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
        return operations[tool](arguments)


def save_plot(plot, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    kind = plot[0]
    if kind == "response":
        _, omega, db, phase, poles, zeros, domain = plot
        fig, axes = plt.subplots(1, 3, figsize=(13, 4), layout="constrained")
        draw = "semilogx" if domain == "analog" else "plot"
        getattr(axes[0], draw)(omega, db)
        getattr(axes[1], draw)(omega, phase)
        axes[0].set(ylabel="Magnitude (dB)", xlabel="Angular frequency", title="Frequency response")
        axes[1].set(ylabel="Unwrapped phase (degrees)", xlabel="Angular frequency")
        axes[2].scatter(zeros.real, zeros.imag, marker="o", facecolors="none", edgecolors="tab:blue", label="Zeros")
        axes[2].scatter(poles.real, poles.imag, marker="x", color="tab:red", label="Poles")
        if domain == "digital":
            angle = np.linspace(0, 2 * math.pi, 256)
            axes[2].plot(np.cos(angle), np.sin(angle), "--", color="gray")
        axes[2].set(xlabel="Real", ylabel="Imaginary", title="Pole-zero plane")
        axes[2].set_aspect("equal", adjustable="datalim")
        axes[2].legend()
    elif kind == "convolution":
        _, indices, values, x, h, x_start, h_start = plot
        fig, axes = plt.subplots(3, 1, figsize=(9, 8), layout="constrained")
        for axis, coords, vals, label in zip(axes, [np.arange(x_start, x_start + len(x)), np.arange(h_start, h_start + len(h)), indices], [x, h, values], ["x[n]", "h[n]", "y[n] = x[n] * h[n]"]):
            axis.stem(coords, vals)
            axis.set(xlabel="Integer index n", ylabel=label)
    elif kind == "fourier_pair":
        _, time, values, spectrum = plot
        fig, axes = plt.subplots(3, 1, figsize=(10, 9), layout="constrained")
        axes[0].plot(time, values)
        axes[0].set(xlabel="t", ylabel="x(t)", title="Time domain: same function and parameters as spectrum")
        if spectrum[0] == "impulses":
            _, impulses, low, high = spectrum
            for axis, component in zip(axes[1:], ["real", "imag"]):
                axis.axhline(0, color="gray", linewidth=.7)
                for item in impulses:
                    if low <= item["omega"] <= high:
                        weight = item[f"weight_{component}"]
                        axis.annotate("", xy=(item["omega"], weight), xytext=(item["omega"], 0), arrowprops={"arrowstyle": "->", "color": "tab:blue", "lw": 2})
                        axis.annotate(f"weight={weight:.5g}", (item["omega"], weight), xytext=(5, 5), textcoords="offset points")
                scale = max([abs(item[f"weight_{component}"]) for item in impulses] + [1.])
                axis.set(xlim=(low, high), ylim=(-1.4 * scale, 1.4 * scale), xlabel="omega (rad/time)", ylabel=f"{component.capitalize()} delta weight")
            axes[1].set_title("Dirac impulses: arrows denote weights, NOT finite heights")
        else:
            _, omega, values, title = spectrum
            axes[1].plot(omega, np.abs(values))
            axes[1].set(title=title, ylabel="|X(omega)|")
            axes[2].plot(omega, values.real, label="Real")
            axes[2].plot(omega, values.imag, label="Imaginary")
            axes[2].set(xlabel="omega (rad/time)", ylabel="X(omega)")
            axes[2].legend()
    else:
        _, grid, values, domain = plot
        fig, axis = plt.subplots(figsize=(9, 4), layout="constrained")
        axes = [axis]
        if domain == "discrete":
            axis.stem(grid, values)
        else:
            axis.plot(grid, values)
        axis.set(xlabel="n" if domain == "discrete" else "t", ylabel="Signal value", title="Sampled waveform")
    for axis in axes:
        axis.grid(True, alpha=.25)
    try:
        fig.savefig(path, format="png", dpi=120)
    finally:
        plt.close(fig)


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(32769))
        result = compute(payload["tool"], payload["arguments"])
        plot = result.pop("plot")
        save_plot(plot, Path(sys.argv[1]))
        result["status"] = "completed"
    except Exception as exc:
        # The parent preserves this concrete error; there is no fabricated fallback.
        result = {"status": "error", "summary": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result, allow_nan=False, ensure_ascii=True))


if __name__ == "__main__":
    main()
