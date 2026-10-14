from __future__ import annotations

import asyncio
import json
import math
import subprocess
import uuid

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import tool_artifacts
from backend.app.services import local_tools
from backend.app.services.local_tools_worker import ToolInputError, compute, expression_values


def _complex(data):
    return np.asarray(data["real"]) + 1j * np.asarray(data["imag"])


def test_continuous_and_discrete_waveforms_use_correct_coordinates():
    continuous = compute("waveform", {"domain": "continuous", "expression": "sin(pi*t)", "start": -1, "stop": 1, "samples": 17})
    np.testing.assert_allclose(continuous["data"]["values"], np.sin(math.pi * np.linspace(-1, 1, 17)))
    discrete = compute("waveform", {"domain": "discrete", "expression": "n**-1", "start": 1, "stop": 3})
    assert discrete["data"]["coordinates"] == [1, 2, 3]
    np.testing.assert_allclose(discrete["data"]["values"], [1, .5, 1 / 3])
    boundaries = expression_values("rect(t)+step(t)", np.array([-.5, 0, .5]))
    np.testing.assert_allclose(boundaries, [.5, 1.5, 1.5])


def test_exact_gaussian_and_shifted_rectangular_fourier():
    args = {"mode": "gaussian", "amplitude": 2, "sigma": .5, "center": 1, "omega_min": -4, "omega_max": 4, "samples": 17}
    result = compute("fourier", args)
    omega = np.asarray(result["data"]["omega"])
    expected = math.sqrt(2 * math.pi) * np.exp(-omega ** 2 / 8) * np.exp(-1j * omega)
    np.testing.assert_allclose(_complex(result["data"]["spectrum"]), expected)
    assert result["data"]["method"] == "analytic"
    time = np.asarray(result["data"]["time"])
    np.testing.assert_allclose(result["data"]["time_values"], 2 * np.exp(-2 * (time - 1) ** 2))
    rect = compute("fourier", {"mode": "rectangular", "amplitude": 3, "width": 2, "center": .75, "omega_min": -math.pi, "omega_max": math.pi, "samples": 17})
    omega = np.asarray(rect["data"]["omega"])
    values = _complex(rect["data"]["spectrum"])
    np.testing.assert_allclose(values, 6 * np.sinc(omega / math.pi) * np.exp(-1j * omega * .75), atol=1e-14)
    assert values[8] == pytest.approx(6)
    assert abs(values[0]) < 1e-12
    time, sampled = rect["data"]["time"], rect["data"]["time_values"]
    area = sum((time[i + 1] - time[i]) * (sampled[i + 1] + sampled[i]) / 2 for i in range(len(time) - 1))
    assert area == pytest.approx(values[8].real)


def test_constant_and_sinusoid_are_exact_weighted_distributions():
    constant = compute("fourier", {"mode": "constant", "amplitude": 2})
    assert constant["data"]["method"] == "ideal_distribution"
    assert constant["data"]["impulses"] == [{"omega": 0., "weight_real": 4 * math.pi, "weight_imag": 0.}]
    np.testing.assert_allclose(constant["data"]["time_values"], 2)
    sine = compute("fourier", {"mode": "sinusoid", "kind": "sin", "omega0": 3, "amplitude": 2})
    impulses = sine["data"]["impulses"]
    assert [impulse["omega"] for impulse in impulses] == [-3, 3]
    assert impulses[0]["weight_imag"] == pytest.approx(2 * math.pi)
    assert impulses[1]["weight_imag"] == pytest.approx(-2 * math.pi)
    np.testing.assert_allclose(sine["data"]["time_values"], 2 * np.sin(3 * np.asarray(sine["data"]["time"])))
    cosine = compute("fourier", {"mode": "sinusoid", "omega0": 0, "phase": math.pi / 3, "amplitude": 2})
    assert len(cosine["data"]["impulses"]) == 1
    assert cosine["data"]["impulses"][0]["weight_real"] == pytest.approx(2 * math.pi)


def test_finite_window_fft_uses_integral_scale_and_time_origin_phase():
    # exp(-t^2/2) on a nonsymmetric interval tests the FFT origin phase correction.
    result = compute("fourier", {"mode": "finite_window", "expression": "exp(-t**2/2)", "start": -7, "stop": 9, "samples": 1024})
    data = result["data"]
    omega = np.asarray(data["omega"])
    spectrum = _complex(data["spectrum"])
    expected = math.sqrt(2 * math.pi) * np.exp(-omega ** 2 / 2)
    selected = np.abs(omega) < 4
    np.testing.assert_allclose(spectrum[selected], expected[selected], atol=1e-10)
    assert spectrum[np.argmin(abs(omega))].real == pytest.approx(math.sqrt(2 * math.pi), abs=1e-10)
    assert data["method"] == "finite_window_fft"
    assert data["time"][-1] < 9


def test_analog_frequency_response_and_poles():
    result = compute("frequency_response", {"domain": "analog", "numerator": [1], "denominator": [1, 1], "omega_min": .01, "omega_max": 100, "samples": 17})
    data = result["data"]
    omega = np.asarray(data["omega"])
    np.testing.assert_allclose(_complex(data["response"]), 1 / (1 + 1j * omega))
    np.testing.assert_allclose(data["magnitude_db"], -10 * np.log10(1 + omega ** 2))
    np.testing.assert_allclose(data["phase_degrees"], -np.arctan(omega) * 180 / math.pi)
    np.testing.assert_allclose(_complex(data["poles"]), [-1])
    assert data["zeros"] == {"real": [], "imag": []}


def test_digital_response_coefficient_order_delay_and_zero_phase():
    result = compute("frequency_response", {"domain": "digital", "numerator": [1], "denominator": [1, -.5], "samples": 17})
    data = result["data"]
    omega = np.asarray(data["omega"])
    np.testing.assert_allclose(_complex(data["response"]), 1 / (1 - .5 * np.exp(-1j * omega)))
    np.testing.assert_allclose(_complex(data["poles"]), [.5])
    np.testing.assert_allclose(_complex(data["zeros"]), [0])
    delay = compute("frequency_response", {"domain": "digital", "numerator": [0, 1], "denominator": [1], "samples": 17})
    np.testing.assert_allclose(_complex(delay["data"]["response"]), np.exp(-1j * np.asarray(delay["data"]["omega"])))
    np.testing.assert_allclose(_complex(delay["data"]["poles"]), [0])
    assert delay["data"]["zeros"] == {"real": [], "imag": []}
    zero = compute("frequency_response", {"domain": "digital", "numerator": [1, -1], "denominator": [1], "samples": 17})
    assert zero["data"]["magnitude"][0] == 0
    assert zero["data"]["magnitude_db"][0] is None
    assert zero["data"]["phase_degrees"][0] is None
    nyquist_zero = compute("frequency_response", {"domain": "digital", "numerator": [1, 1], "denominator": [1], "samples": 17})
    assert nyquist_zero["data"]["magnitude"][-1] == 0
    assert nyquist_zero["data"]["phase_degrees"][-1] is None
    tiny_gain = compute("frequency_response", {"domain": "digital", "numerator": [1e-16], "denominator": [1], "samples": 17})
    np.testing.assert_allclose(tiny_gain["data"]["magnitude_db"], [-320] * 17)


def test_convolution_preserves_nonzero_and_negative_index_origins():
    result = compute("convolution", {"x": [1, 2, 3], "h": [1, -1], "x_start": -2, "h_start": 4})
    assert result["data"]["indices"] == [2, 3, 4, 5]
    assert result["data"]["values"] == [1, 1, 1, -3]
    assert result["data"]["x_indices"] == [-2, -1, 0]
    assert result["data"]["h_indices"] == [4, 5]


@pytest.mark.parametrize("expression", [
    "__import__('os').system('whoami')", "open('secret').read()", "t.__class__", "t[0]", "[t]",
    "(lambda: 1)()", "sin(t, t)", "sin(x=t)", "t > 0", "True", "1j", "unknown", "n",
    "t**t", "t**100", "t**(-33)", "1/0", "sqrt(-1)", "exp(1000000)",
    " + ".join(["t"] * 70), "(" * 30 + "sin(" * 30 + "t" + ")" * 60,
])
def test_hostile_undefined_or_unbounded_expressions_are_rejected(expression):
    with pytest.raises(ToolInputError):
        expression_values(expression, np.array([0., 1., 2.]))


@pytest.mark.parametrize("tool,arguments", [
    ("waveform", {"domain": "continuous", "expression": "t", "start": 0, "stop": 1, "samples": 4097}),
    ("waveform", {"domain": "discrete", "expression": "n", "start": 0, "stop": 4096}),
    ("waveform", {"domain": "discrete", "expression": "n", "start": 0, "stop": 1, "samples": 16}),
    ("fourier", {"mode": "gaussian", "sigma": 0}),
    ("fourier", {"mode": "unsupported"}),
    ("fourier", {"mode": "constant", "expression": "t"}),
    ("frequency_response", {"domain": "analog", "numerator": [1], "denominator": [0]}),
    ("frequency_response", {"domain": "analog", "numerator": [0], "denominator": [1]}),
    ("frequency_response", {"domain": "digital", "numerator": [1], "denominator": [1, -1]}),
    ("frequency_response", {"domain": "digital", "numerator": [1], "denominator": [0, 1]}),
    ("convolution", {"x": [float("nan")], "h": [1]}),
    ("convolution", {"x": [True], "h": [1]}),
    ("convolution", {"x": [10 ** 400], "h": [1]}),
    ("convolution", {"x": [1] * 257, "h": [1]}),
    ("convolution", {"x": [1], "h": [1], "path": "outside.png"}),
])
def test_invalid_inputs_and_sampled_poles_are_explicit_errors(tool, arguments):
    with pytest.raises(ToolInputError):
        compute(tool, arguments)


def test_real_workers_generate_persistable_pngs_and_replayable_media(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "must-not-reach-worker")
    requests = [
        {"tool": "waveform", "arguments": {"domain": "discrete", "expression": "cos(pi*n)", "start": -2, "stop": 2}},
        {"tool": "fourier", "arguments": {"mode": "constant", "amplitude": 2}},
        {"tool": "frequency_response", "arguments": {"domain": "analog", "numerator": [1], "denominator": [1, 1], "samples": 16}},
        {"tool": "convolution", "arguments": {"x": [1, 2], "h": [1, -1], "x_start": -1, "h_start": 2}},
    ]
    directory = tmp_path / "artifacts"
    results = asyncio.run(local_tools.execute_tool_requests(requests, artifact_dir=directory))
    assert len(results) == 4
    assert all(result["status"] == "completed" for result in results), results
    assert results[3]["data"]["values"] == [1, 1, -2]
    assert results[3]["data"]["indices"] == [1, 2, 3]
    stored = json.loads(json.dumps(results, allow_nan=False))
    app = FastAPI()
    app.include_router(tool_artifacts.router)
    app.dependency_overrides[tool_artifacts.get_artifact_directory] = lambda: directory
    with TestClient(app) as client:
        for result in stored:
            artifact = result["artifacts"][0]
            assert uuid.UUID(hex=artifact["id"]).hex == artifact["id"]
            disk_image = (directory / f"{artifact['id']}.png").read_bytes()
            assert disk_image.startswith(local_tools.PNG_SIGNATURE)
            response = client.get(artifact["url"])
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.content == disk_image


def test_artifact_router_rejects_nonopaque_ids_missing_files_and_non_png(tmp_path):
    app = FastAPI()
    app.include_router(tool_artifacts.router)
    app.dependency_overrides[tool_artifacts.get_artifact_directory] = lambda: tmp_path
    invalid_image_id = uuid.uuid4().hex
    (tmp_path / f"{invalid_image_id}.png").write_bytes(b"not a PNG")
    with TestClient(app) as client:
        for artifact_id in ["not-an-id", "A" * 32, "0" * 31, "0" * 33, uuid.uuid4().hex, invalid_image_id, "..%2Fsecret"]:
            assert client.get(f"/api/tool-artifacts/{artifact_id}").status_code == 404


def test_artifact_router_rejects_symlinks(tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(local_tools.PNG_SIGNATURE + b"outside")
    directory = tmp_path / "artifacts"
    directory.mkdir()
    artifact_id = uuid.uuid4().hex
    try:
        (directory / f"{artifact_id}.png").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires platform permission")
    app = FastAPI()
    app.include_router(tool_artifacts.router)
    app.dependency_overrides[tool_artifacts.get_artifact_directory] = lambda: directory
    with TestClient(app) as client:
        assert client.get(f"/api/tool-artifacts/{artifact_id}").status_code == 404


def test_worker_error_is_preserved_without_artifact(tmp_path):
    results = asyncio.run(local_tools.execute_tool_requests([
        {"tool": "waveform", "arguments": {"domain": "continuous", "expression": "__import__('os')", "start": 0, "stop": 1, "samples": 16}},
    ], artifact_dir=tmp_path))
    assert results[0]["status"] == "error"
    assert results[0]["artifacts"] == []
    assert list(tmp_path.iterdir()) == []


def test_request_limit_nonfinite_values_and_deadline_are_preserved_errors(tmp_path, monkeypatch):
    requests = [{"tool": "convolution", "arguments": {"x": [1], "h": [1]}}] * 5
    result = asyncio.run(local_tools.execute_tool_requests(requests, artifact_dir=tmp_path))
    assert len(result) == 1 and result[0]["status"] == "error"
    invalid = asyncio.run(local_tools.execute_tool_requests([{"tool": "convolution", "arguments": {"x": [math.inf], "h": [1]}}], artifact_dir=tmp_path))
    assert invalid[0]["status"] == "error"

    def timeout_worker(command, **kwargs):
        assert command[1] == "-I"
        assert kwargs["timeout"] == local_tools.WORKER_TIMEOUT_SECONDS
        assert "LLM_API_KEY" not in kwargs["env"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(local_tools.subprocess, "run", timeout_worker)
    timeout = asyncio.run(local_tools.execute_tool_requests(requests[:1], artifact_dir=tmp_path))
    assert timeout[0]["status"] == "error"
    assert timeout[0]["artifacts"] == []


def test_worker_environment_has_no_provider_or_python_startup_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "hostile")
    monkeypatch.setenv("PYTHONSTARTUP", "hostile.py")
    environment = local_tools._worker_environment(tmp_path)
    assert not {"LLM_API_KEY", "PYTHONPATH", "PYTHONSTARTUP"} & set(environment)




def test_sampled_output_bound_retains_aligned_actual_points_and_full_convolution():
    original = compute("frequency_response", {"domain": "digital", "numerator": [1], "denominator": [1, -.5], "samples": 4096})["data"]
    data = json.loads(json.dumps(original))
    assert local_tools._bound_sampled_data(data) is True
    metadata = data["sampling"]["omega"]
    assert metadata["computed_count"] == 4096
    assert metadata["returned_count"] == 128
    selected = metadata["source_indices"]
    assert selected[0] == 0 and selected[-1] == 4095
    assert len(set(selected)) == 128
    for key in ["omega", "magnitude", "magnitude_db", "phase_degrees"]:
        assert data[key] == [original[key][index] for index in selected]
    for component in ["real", "imag"]:
        assert data["response"][component] == [original["response"][component][index] for index in selected]
    convolution = compute("convolution", {"x": [1] * 256, "h": [1] * 256})["data"]
    assert local_tools._bound_sampled_data(convolution) is False
    assert len(convolution["values"]) == 511
    assert convolution["values"][255] == 256
