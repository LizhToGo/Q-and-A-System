"""Provider-independent, bounded local computations with persistent PNG artifacts."""
from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parents[3] / "data" / "tool_artifacts"
WORKER_PATH = Path(__file__).with_name("local_tools_worker.py")
WORKER_TIMEOUT_SECONDS = 25
MAX_REQUEST_BYTES = 32768
TOOLS = {"waveform", "fourier", "frequency_response", "convolution"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _object(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _number(default=None, minimum=-1e6, maximum=1e6):
    schema = {"type": "number", "minimum": minimum, "maximum": maximum}
    if default is not None:
        schema["default"] = default
    return schema


def tool_instructions() -> str:
    """Exact JSON request schemas and numerical conventions for the answering model."""
    sample = {"type": "integer", "minimum": 16, "maximum": 4096, "default": 1024}
    expression = {"type": "string", "minLength": 1, "maxLength": 512}
    index = {"type": "integer", "minimum": -1000000, "maximum": 1000000, "default": 0}
    waveform = {"oneOf": [
        _object({"domain": {"const": "continuous"}, "expression": expression, "start": _number(), "stop": _number(), "samples": sample}, ["domain", "expression", "start", "stop"]),
        _object({"domain": {"const": "discrete"}, "expression": expression, "start": index, "stop": index}, ["domain", "expression", "start", "stop"]),
    ]}
    ideal = {"samples": sample, "omega_min": _number(-20), "omega_max": _number(20), "amplitude": _number(1)}
    fourier = {"oneOf": [
        _object({"mode": {"const": "gaussian"}, **ideal, "sigma": _number(1, 1e-6), "center": _number(0)}, ["mode"]),
        _object({"mode": {"const": "rectangular"}, **ideal, "width": _number(1, 1e-6), "center": _number(0)}, ["mode"]),
        _object({"mode": {"const": "constant"}, **ideal}, ["mode"]),
        _object({"mode": {"const": "sinusoid"}, **ideal, "omega0": _number(1, 0), "phase": _number(0), "kind": {"enum": ["cos", "sin"], "default": "cos"}}, ["mode"]),
        _object({"mode": {"const": "finite_window"}, "expression": expression, "start": _number(), "stop": _number(), "samples": sample}, ["mode", "expression", "start", "stop"]),
    ]}
    coefficients = {"type": "array", "items": _number(), "minItems": 1, "maxItems": 17}
    frequency = {"oneOf": [
        _object({"domain": {"const": "analog"}, "numerator": coefficients, "denominator": coefficients, "omega_min": _number(.01, 1e-8), "omega_max": _number(100, 1e-8), "samples": sample}, ["domain", "numerator", "denominator"]),
        _object({"domain": {"const": "digital"}, "numerator": coefficients, "denominator": coefficients, "omega_min": _number(0, 0, math.pi), "omega_max": _number(math.pi, 0, math.pi), "samples": sample}, ["domain", "numerator", "denominator"]),
    ]}
    values = {"type": "array", "items": _number(), "minItems": 1, "maxItems": 256}
    convolution = _object({"x": values, "h": values, "x_start": index, "h_start": index}, ["x", "h"])
    schemas = {"type": "array", "maxItems": 4, "items": {"oneOf": [
        _object({"tool": {"const": name}, "arguments": schema}, ["tool", "arguments"])
        for name, schema in [("waveform", waveform), ("fourier", fourier), ("frequency_response", frequency), ("convolution", convolution)]
    ]}}
    return (
        "Optional tool_requests must follow this exact JSON Schema (no code, shell, paths or URLs):\n"
        + json.dumps(schemas, ensure_ascii=False)
        + "\nRules: at most 4 requests; all numbers finite and bounded by schemas; request JSON <=32768 UTF-8 bytes. "
        "Expressions are real, <=128 AST nodes and <=24 nesting levels, using t (continuous/Fourier) or n (discrete), pi, e, "
        "+ - * / **, parentheses and single-argument sin/cos/tan/exp/sqrt/log/abs/sinc/sign/rect/step only. "
        "Mathematical ^ is also accepted as exponentiation (not XOR); prefer ** in generated expressions. "
        "Exponent must be a numeric literal in [-32,32]. No attributes, subscripts, comparisons, Python code or unknown names. "
        "sinc(x)=sin(pi*x)/(pi*x), rect(x)=1 for |x|<1/2, half at boundaries; step(0)=1/2. "
        "Continuous start<stop, length<=1000000, inclusive display samples. Discrete start<=stop, integer inclusive indices, "
        "<=4096 points, omit samples. Fourier uses X(omega)=integral x(t)exp(-j*omega*t)dt, inverse factor 1/(2*pi). "
        "Ideal gaussian is A*exp(-(t-center)^2/(2*sigma^2)); rectangular is A*rect((t-center)/width). "
        "constant is A; sinusoid is A*cos(omega0*t+phase) or A*sin(omega0*t+phase), phase in radians. "
        "These ideal spectra are analytic; constant/sinusoid return Dirac delta weights, NOT finite impulse heights. "
        "Each fourier operation produces a matched time-domain/spectrum figure from the SAME family and parameters; "
        "do not request separate waveform plots for a before/after comparison. Prefer supported exact families for basic examples. "
        "Convolution questions MUST use convolution to calculate outputs, never hard-code guessed output values into waveform plots. "
        "omega_min<omega_max. finite_window uses expression on [start,stop) and FFT rectangle-rule approximation: "
        "explicitly call it finite-window, not an exact infinite-time transform. Unsupported exact transforms must use a labelled "
        "finite_window request or state unsupported; do not pretend sampled FFT is exact. "
        "Analog frequency_response coefficients are descending powers of s, H(j*omega), 0<omega_min<omega_max. "
        "Digital coefficients are increasing powers of z^-1, H(exp(j*omega)), 0<=omega_min<omega_max<=pi; denominator[0]!=0. "
        "Nonzero numerator and denominator required; the zero transfer function is not supported by this frequency-response tool. "
        "Poles on sampled frequencies are errors. Common factors are not cancelled; "
        "do not claim stability/ROC without conditions. Discrete convolution uses finite sequences with index origins; output "
        "origin=x_start+h_start, full linear convolution, zero outside input support. "
        "Plots use all computed points; sampled result arrays are aligned previews capped at 128 points with source indices "
        "and original counts in data.sampling. Convolution retains its entire <=511-point output. "
        "Explain actual completed results and notes; preserve tool errors. Never invent artifact URLs or calculated data.\n"
        "Examples: " + json.dumps([
            {"tool": "waveform", "arguments": {"domain": "continuous", "expression": "exp(-abs(t))*cos(3*t)", "start": -5, "stop": 5}},
            {"tool": "fourier", "arguments": {"mode": "sinusoid", "amplitude": 2, "omega0": 3, "kind": "cos"}},
            {"tool": "frequency_response", "arguments": {"domain": "analog", "numerator": [1], "denominator": [1, 1]}},
            {"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1], "x_start": -1, "h_start": 2}},
        ], ensure_ascii=False)
    )


def _error(tool, parameters, message):
    return {"id": uuid.uuid4().hex, "tool": tool, "status": "error", "title": "本地计算失败", "summary": message, "parameters": parameters, "artifacts": [], "data": {}, "notes": ["本次未产生成功的数值结果或图像；工具结果不作为教材依据。"]}


def _bound_sampled_data(data):
    """Retain aligned actual computed samples, not a second approximate calculation."""
    groups = {
        "coordinates": ["coordinates", "values"],
        "omega": ["omega", "spectrum", "response", "magnitude", "magnitude_db", "phase_degrees"],
        "time": ["time", "time_values"],
    }
    sampling = {}
    for coordinate, keys in groups.items():
        values = data.get(coordinate)
        if not isinstance(values, list) or len(values) <= 128:
            continue
        count = len(values)
        selected = [round(i * (count - 1) / 127) for i in range(128)]
        # Keep the closest-to-zero sample useful for DC/origin interpretation.
        center = min(range(count), key=lambda index: abs(values[index]))
        if center not in selected:
            replace = min(range(1, 127), key=lambda index: abs(selected[index] - center))
            selected[replace] = center
            selected.sort()
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                data[key] = [value[index] for index in selected]
            elif isinstance(value, dict) and set(value) == {"real", "imag"}:
                data[key] = {part: [array[index] for index in selected] for part, array in value.items()}
        sampling[coordinate] = {"computed_count": count, "returned_count": len(selected), "source_indices": selected}
    if sampling:
        data["sampling"] = sampling
    return bool(sampling)


def _worker_environment(directory):
    # Never forward provider/API secrets or Python startup overrides to the worker.
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot", "WINDIR") if key in os.environ}
    environment.update({"TEMP": str(directory), "TMP": str(directory), "TMPDIR": str(directory), "MPLCONFIGDIR": str(directory / "mpl"), "HOME": str(directory), "USERPROFILE": str(directory), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    return environment


def _execute_one(request, artifact_dir):
    if not isinstance(request, dict):
        return _error("unknown", {}, "Tool request must be an object")
    tool = request.get("tool")
    parameters = request.get("arguments")
    if not isinstance(tool, str) or tool not in TOOLS:
        return _error("unknown", {}, "Unknown local tool")
    if set(request) != {"tool", "arguments"} or not isinstance(parameters, dict):
        return _error(tool, {}, "Expected exactly tool and object arguments")
    try:
        serialized = json.dumps(request, allow_nan=False, ensure_ascii=True).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        return _error(tool, {}, "Tool request must be finite, JSON-serializable data")
    if len(serialized) > MAX_REQUEST_BYTES:
        return _error(tool, {}, "Tool request exceeds 32768 bytes")
    # JSON round-trip ensures the persisted parameter values are independent of caller mutation.
    parameters = json.loads(serialized)["arguments"]
    artifact_id = uuid.uuid4().hex
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="local-tool-") as temporary:
            directory = Path(temporary)
            staged = directory / "result.png"
            completed = subprocess.run(
                [sys.executable, "-I", str(WORKER_PATH), str(staged)],
                input=serialized, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=directory, env=_worker_environment(directory), timeout=WORKER_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode != 0:
                # Worker stderr cannot contain inherited credentials (environment is allowlisted).
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[-2000:]
                return _error(tool, parameters, f"Worker exited with code {completed.returncode}: {detail or 'no diagnostic'}")
            if len(completed.stdout) > 2 * 1024 * 1024:
                return _error(tool, parameters, "Worker result exceeded the output limit")
            result = json.loads(completed.stdout)
            if result.get("status") != "completed":
                return _error(tool, parameters, result.get("summary", "Worker did not complete"))
            image = staged.read_bytes()
            if not image.startswith(PNG_SIGNATURE) or len(image) > 8 * 1024 * 1024:
                return _error(tool, parameters, "Worker did not produce a valid bounded PNG artifact")
            # Publish only a finished PNG under an opaque server-generated identifier.
            destination = artifact_dir / f"{artifact_id}.png"
            with destination.open("xb") as handle:
                handle.write(image)
        if _bound_sampled_data(result["data"]):
            result["notes"].append("图像使用完整采样网格。返回的数据预览保留最多128个同索引实际采样点，包含端点及最接近零的点；data.sampling 记录原始索引和完整点数。预览不是重新计算的低分辨率变换。")
        return {"id": uuid.uuid4().hex, "tool": tool, "status": "completed", "title": result["title"], "summary": result["summary"], "parameters": parameters, "artifacts": [{"id": artifact_id, "url": f"/api/tool-artifacts/{artifact_id}", "caption": result["title"]}], "data": result["data"], "notes": result["notes"]}
    except subprocess.TimeoutExpired:
        return _error(tool, parameters, f"Local computation exceeded the fixed {WORKER_TIMEOUT_SECONDS}-second deadline")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _error(tool, parameters, f"{type(exc).__name__}: {exc}")


async def execute_tool_requests(requests: list[dict], *, artifact_dir: Path | str | None = None) -> list[dict]:
    """Execute at most four fixed operations, preserving errors and replayable artifacts.

    artifact_dir is a trusted application/test override, never a model request field.
    Offloading subprocess.run works with Windows event-loop implementations too.
    """
    if not isinstance(requests, list) or len(requests) > 4:
        return [_error("unknown", {}, "tool_requests must be a list with at most 4 requests; none were executed")]
    directory = Path(artifact_dir) if artifact_dir is not None else ARTIFACT_DIR
    # Sequential workers bound per-call CPU/memory; each process has its own fixed deadline.
    results = []
    for request in requests:
        results.append(await asyncio.to_thread(_execute_one, request, directory))
    return results
