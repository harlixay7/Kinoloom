"""Fused W4A8 GEMM numerical parity gate (ported from the LTX-2.5 lab).

Why this exists: a silent fallback to eager dequantization would still produce
video, just slower, and would invalidate every performance number downstream.
The gate answers three questions in one run:

  1. Is the comfy-kitchen CUDA backend registered and selected for
     w4a8_int8_linear on this machine?
  2. Does the fused kernel agree with an fp32 dequantized-weight reference
     (0.96% relative output error in the layer-level reference test; 9.7e-3 worst
     observed error among sampled benchmark layers) and with a same-activation
     int8 reference, which is the decisive test?
  3. What is the synthetic fusion speedup at m=1024, n=k=4096?

Exit codes: 0 gate PASS, 2 gate FAIL, 3 environment (torch / comfy-kitchen
missing or CUDA unavailable).

Usage:
  python bench/gate_w4a8.py --out runs/gate_w4a8.json [--comfyui PATH]

--comfyui adds a ComfyUI-core registration check. It is optional; without it
that section is reported SKIPPED, not failed. All other sections need only
comfy-kitchen.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

FAILURES: list[str] = []


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def check(cond: bool, msg: str) -> bool:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILURES.append(msg)
    return cond


def main() -> int:
    ap = argparse.ArgumentParser(description="W4A8 fused-kernel parity gate")
    ap.add_argument("--out", type=str, required=True,
                    help="path for the JSON evidence record")
    ap.add_argument("--comfyui", type=str, default=None,
                    help="optional path to a ComfyUI checkout for the native-layout check")
    args = ap.parse_args()

    record: dict = {"gate": "UNKNOWN", "failures": []}

    # ------------------------------------------------------------- environment
    section("1. Environment")
    try:
        import psutil
        import torch
    except ImportError as exc:
        print(f"SKIP  torch/psutil unavailable: {exc}")
        record["gate"] = "ENVIRONMENT"
        record["error"] = str(exc)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(record, indent=2), encoding="utf-8")
        return 3

    record["torch"] = torch.__version__
    record["torch_cuda_runtime"] = torch.version.cuda
    record["cuda_available"] = torch.cuda.is_available()
    if not record["cuda_available"]:
        print("SKIP  torch cannot see CUDA")
        record["gate"] = "ENVIRONMENT"
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(record, indent=2), encoding="utf-8")
        return 3

    record["gpu"] = torch.cuda.get_device_name(0)
    record["compute_capability"] = list(torch.cuda.get_device_capability(0))
    record["torch_arch_list"] = torch.cuda.get_arch_list()
    props = torch.cuda.get_device_properties(0)
    record["vram_total_gib"] = round(props.total_memory / 2**30, 3)
    record["multi_processor_count"] = props.multi_processor_count
    record["smem_per_block_optin"] = getattr(props, "shared_memory_per_block_optin", None)
    vm = psutil.virtual_memory()
    record["ram_total_gib"] = round(vm.total / 2**30, 2)
    record["ram_available_gib"] = round(vm.available / 2**30, 2)
    for key, value in record.items():
        print(f"  {key}: {value}")
    check(record["cuda_available"], "CUDA available to torch")
    check(tuple(record["compute_capability"]) == (8, 6),
          f"compute capability is (8,6), got {tuple(record['compute_capability'])}")

    # --------------------------------------------------- comfy-kitchen import
    section("2. comfy-kitchen import + backend registration")
    try:
        import comfy_kitchen as ck
        from comfy_kitchen.registry import registry
        import comfy_kitchen.backends.cuda as ck_cuda
    except ImportError as exc:
        print(f"SKIP  comfy-kitchen unavailable: {exc}")
        record["gate"] = "ENVIRONMENT"
        record["error"] = str(exc)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(record, indent=2), encoding="utf-8")
        return 3

    record["comfy_kitchen_version"] = __import__(
        "importlib.metadata", fromlist=["version"]).version("comfy-kitchen")
    backends = ck.list_backends()
    record["list_backends"] = {
        name: {"available": info["available"], "disabled": info["disabled"],
               "unavailable_reason": info["unavailable_reason"]}
        for name, info in backends.items()}
    record["cuda_capabilities_count"] = len(backends.get("cuda", {}).get("capabilities", []))
    print(f"  comfy-kitchen: {record['comfy_kitchen_version']}")
    for name, info in record["list_backends"].items():
        print(f"  backend {name}: available={info['available']} disabled={info['disabled']} "
              f"reason={info['unavailable_reason']}")
    check(backends.get("cuda", {}).get("available", False), "CUDA backend registered/available")
    check("w4a8_int8_linear" in backends.get("cuda", {}).get("capabilities", []),
          "CUDA backend advertises w4a8_int8_linear")
    check("quantize_w4a8_int8_weight" in backends.get("cuda", {}).get("capabilities", []),
          "CUDA backend advertises quantize_w4a8_int8_weight")

    _C = ck_cuda._C
    record["cuda_ext_available"] = bool(getattr(ck_cuda, "_EXT_AVAILABLE", False))
    record["has_cublaslt"] = bool(getattr(ck_cuda, "_CUBLASLT_AVAILABLE", False))
    record["w4a8_chunked_env_default"] = bool(getattr(ck_cuda, "_W4A8_CHUNKED", False))
    record["c_has_w4a8_codebook_linear_chunked"] = hasattr(_C, "w4a8_codebook_linear_chunked")
    record["c_has_w4a8_codebook_gemm_chunked"] = hasattr(_C, "w4a8_codebook_gemm_chunked")
    record["c_has_quantize_w4a8_convrot"] = hasattr(_C, "quantize_w4a8_convrot")
    record["c_has_cutlass_int8_dequant"] = hasattr(_C, "cutlass_int8_dequant")
    for key in [k for k in record if k.startswith(("cuda_ext", "has_cublaslt", "w4a8_chunked", "c_has"))]:
        print(f"  {key}: {record[key]}")
    check(record["cuda_ext_available"], "comfy-kitchen CUDA extension loaded")
    check(record["c_has_w4a8_codebook_linear_chunked"] or record["c_has_w4a8_codebook_gemm_chunked"],
          "CUDA extension exposes a W4A8 chunked GEMM entry point")

    # ------------------------------------ ComfyUI native layout registration
    section("3. ComfyUI core native layout registration (optional)")
    import importlib.util
    comfyui = args.comfyui or __import__("os").environ.get("VIDEOLAB_COMFYUI")
    if not comfyui:
        print("SKIP  no --comfyui path and VIDEOLAB_COMFYUI unset")
        record["comfyui_layout_check"] = "SKIPPED"
    elif importlib.util.find_spec("comfy") is None and \
            not (Path(comfyui) / "comfy" / "quant_ops.py").exists():
        print(f"SKIP  comfy package not importable and {comfyui} does not contain comfy/quant_ops.py")
        record["comfyui_layout_check"] = "SKIPPED"
    else:
        sys.path.insert(0, str(comfyui))
        import comfy.quant_ops as qops
        record["comfy_quant_ops_ck_available"] = bool(qops._CK_AVAILABLE)
        record["comfy_layout_is_ck_class"] = qops.AsymW4A8Int8Layout is ck.tensor.AsymW4A8Int8Layout
        record["comfy_layout_min_sm"] = list(qops.AsymW4A8Int8Layout.MIN_SM_VERSION)
        algo = qops.QUANT_ALGOS.get("asym_w4a8_int8")
        record["quant_algos_asym_w4a8_int8"] = None if algo is None else {k: str(v) for k, v in algo.items()}
        registered = qops.get_layout_class("AsymW4A8Int8Layout")
        record["registered_layout_class"] = None if registered is None else registered.__name__
        print(f"  _CK_AVAILABLE: {record['comfy_quant_ops_ck_available']}")
        print(f"  AsymW4A8Int8Layout is comfy_kitchen class: {record['comfy_layout_is_ck_class']}")
        print(f"  registered layout class: {record['registered_layout_class']}")
        check(record["comfy_quant_ops_ck_available"], "ComfyUI imported comfy_kitchen (no stub fallback)")
        check(record["comfy_layout_is_ck_class"],
              "ComfyUI AsymW4A8Int8Layout resolves to the comfy-kitchen class, not the stub")
        check(algo is not None and algo.get("comfy_tensor_layout") == "AsymW4A8Int8Layout",
              "QUANT_ALGOS maps asym_w4a8_int8 to the native layout")
        check(registered is ck.tensor.AsymW4A8Int8Layout,
              "layout registry returns the comfy-kitchen class")

    # ---------------------------------------------------- backend selection
    section("4. Backend selection for w4a8_int8_linear")
    GROUP_SIZE = 16
    CONVROT = 256
    N, K, M = 4096, 4096, 1024
    sel_kwargs = {
        "x": torch.empty(M, K, dtype=torch.bfloat16, device="cuda"),
        "qdata": torch.empty(N, K // 2, dtype=torch.int8, device="cuda"),
        "s_rel": torch.empty(N, K // GROUP_SIZE, dtype=torch.float8_e4m3fn, device="cuda"),
        "s_channel": torch.empty(N, dtype=torch.float32, device="cuda"),
        "codebook": torch.empty(16, dtype=torch.float32, device="cuda"),
        "correction": None,
        "bias": None,
        "group_size": GROUP_SIZE,
        "convrot_groupsize": CONVROT,
        "out_dtype": torch.bfloat16,
    }
    try:
        selected = registry.get_capable_backend("w4a8_int8_linear", kwargs=sel_kwargs)
    except Exception as exc:  # noqa: BLE001
        selected = f"ERROR: {type(exc).__name__}: {exc}"
    record["selected_backend_w4a8_linear"] = selected
    print(f"  selected backend for w4a8_int8_linear: {selected}")
    check(selected == "cuda", f"registry selects the CUDA backend (got {selected})")

    # ------------------------------------------------ synthetic correctness
    section("5. Synthetic W4A8 correctness + timing")
    from comfy_kitchen.tensor.w4a8_int8 import (  # noqa: E402
        quantize_w4a8_int8_weight,
        dequantize_w4a8_int8_weight,
        w4a8_int8_linear,
    )

    torch.manual_seed(1234)
    gen = torch.Generator(device="cuda").manual_seed(1234)
    W = (torch.randn(N, K, generator=gen, device="cuda", dtype=torch.float32) * 0.02)
    x = torch.randn(M, K, generator=gen, device="cuda", dtype=torch.bfloat16) * 1.0

    qdata, s_rel, s_channel, correction, codebook = quantize_w4a8_int8_weight(
        W, group_size=GROUP_SIZE, convrot_groupsize=CONVROT, symmetric=True,
        scale_dtype=torch.float8_e4m3fn, codebook=True,
    )
    meta = {
        "qdata": {"shape": list(qdata.shape), "dtype": str(qdata.dtype)},
        "s_rel": {"shape": list(s_rel.shape), "dtype": str(s_rel.dtype)},
        "s_channel": {"shape": list(s_channel.shape), "dtype": str(s_channel.dtype)},
        "codebook": None if codebook is None else {"shape": list(codebook.shape), "dtype": str(codebook.dtype)},
        "correction_is_none": correction is None,
    }
    record["quantized_metadata"] = meta
    print(f"  qdata {meta['qdata']}")
    print(f"  s_rel {meta['s_rel']}")
    print(f"  s_channel {meta['s_channel']}")
    print(f"  codebook {meta['codebook']}")
    check(meta["qdata"]["dtype"] == "torch.int8" and meta["qdata"]["shape"] == [N, K // 2],
          "packed weight is int8 [N, K/2]")
    check(meta["s_rel"]["dtype"] == "torch.float8_e4m3fn" and meta["s_rel"]["shape"] == [N, K // GROUP_SIZE],
          "s_rel is fp8 [N, K/group_size]")
    check(meta["s_channel"]["shape"] == [N], "s_channel is [N]")
    check(codebook is not None and list(codebook.shape) == [16], "codebook is (16,)")

    W_deq = dequantize_w4a8_int8_weight(
        qdata, s_rel, s_channel, codebook=codebook, correction=correction,
        group_size=GROUP_SIZE, convrot_groupsize=CONVROT, output_dtype=torch.float32,
    )
    ref_fp32 = x.float() @ W_deq.t()

    with registry.use_backend("eager"):
        out_eager = w4a8_int8_linear(x, qdata, s_rel, s_channel, codebook=codebook,
                                     correction=correction, bias=None, group_size=GROUP_SIZE,
                                     convrot_groupsize=CONVROT, out_dtype=torch.bfloat16)
        print("  eager backend executed under explicit override")

    fallback_calls = {"n": 0}
    _orig_fallback = ck_cuda.eager_w4a8_int8_linear

    def _counting_fallback(*a, **kw):
        fallback_calls["n"] += 1
        return _orig_fallback(*a, **kw)

    ck_cuda.eager_w4a8_int8_linear = _counting_fallback

    cu_kernel_calls = {"n": 0}
    for fname in ("w4a8_codebook_linear_chunked", "w4a8_codebook_gemm_chunked"):
        if hasattr(_C, fname):
            orig = getattr(_C, fname)

            def _wrap(orig=orig, fname=fname):
                def inner(*a, **kw):
                    cu_kernel_calls["n"] += 1
                    cu_kernel_calls.setdefault("last", fname)
                    return orig(*a, **kw)
                return inner
            try:
                setattr(_C, fname, _wrap())
                print(f"  instrumented _C.{fname}")
            except Exception as exc:  # noqa: BLE001
                print(f"  could not instrument _C.{fname}: {exc}")

    out_cuda = w4a8_int8_linear(x, qdata, s_rel, s_channel, codebook=codebook,
                                correction=correction, bias=None, group_size=GROUP_SIZE,
                                convrot_groupsize=CONVROT, out_dtype=torch.bfloat16)

    record["eager_fallback_calls_during_cuda_run"] = fallback_calls["n"]
    record["cuda_kernel_calls"] = cu_kernel_calls["n"]
    record["cuda_kernel_last"] = cu_kernel_calls.get("last")
    print(f"  eager fallback calls during CUDA run: {fallback_calls['n']}")
    print(f"  cuda w4a8 kernel calls: {cu_kernel_calls['n']} (last={cu_kernel_calls.get('last')})")
    check(fallback_calls["n"] == 0, "CUDA call did NOT silently fall back to eager")

    def rel_l2(a: torch.Tensor, b: torch.Tensor) -> float:
        return ((a.float() - b.float()).norm() / b.float().norm().clamp_min(1e-12)).item()

    record["cuda_vs_eager_rel_l2"] = rel_l2(out_cuda, out_eager)
    record["cuda_vs_eager_max_abs"] = (out_cuda.float() - out_eager.float()).abs().max().item()
    record["cuda_vs_fp32_ref_rel_l2"] = rel_l2(out_cuda, ref_fp32)
    record["eager_vs_fp32_ref_rel_l2"] = rel_l2(out_eager, ref_fp32)
    cos = torch.nn.functional.cosine_similarity(
        out_cuda.float().flatten(), ref_fp32.flatten(), dim=0).item()
    record["cuda_vs_fp32_ref_cosine"] = cos
    print(f"  CUDA vs eager      relL2={record['cuda_vs_eager_rel_l2']:.6e} "
          f"maxabs={record['cuda_vs_eager_max_abs']:.6e}")
    print(f"  CUDA  vs fp32 ref  relL2={record['cuda_vs_fp32_ref_rel_l2']:.6e} cosine={cos:.8f}")
    print(f"  eager vs fp32 ref  relL2={record['eager_vs_fp32_ref_rel_l2']:.6e}")
    # Cross-backend agreement is bounded loosely ON PURPOSE: the eager path rotates activations
    # in bf16 while the fused CUDA kernel rotates/quantizes them itself, so the two disagree by
    # activation-quantization noise, not by kernel error (0.96% relative output error in the
    # layer-level reference test against FP32 dequantized weights; 9.7e-3 worst observed error
    # across sampled layers). The decisive kernel test is section 5b, which feeds both the same
    # int8 activations.
    check(record["cuda_vs_eager_rel_l2"] < 2e-2,
          f"CUDA agrees with eager backend within 2e-2 relL2 (got {record['cuda_vs_eager_rel_l2']:.3e})")
    check(record["cuda_vs_fp32_ref_rel_l2"] < 0.05,
          f"CUDA vs fp32 reference relL2 < 0.05 (got {record['cuda_vs_fp32_ref_rel_l2']:.3e})")
    check(cos > 0.999, f"cosine similarity vs fp32 reference > 0.999 (got {cos:.6f})")

    section("5b. Kernel-exact correctness (same int8 activations for both sides)")
    stream = torch.cuda.current_stream().cuda_stream
    x_2d = x.reshape(-1, K).contiguous()
    xq = torch.empty(M, K, dtype=torch.int8, device="cuda")
    xs = torch.empty(M, 1, dtype=torch.float32, device="cuda")
    _C.quantize_int8_rowwise_convrot(x_2d, xq, xs, CONVROT, False, 0, stream)
    w8 = torch.empty(N, K, dtype=torch.int8, device="cuda")
    _C.dequant_int4_grouped_to_int8_e4m3(qdata.contiguous(), s_rel.contiguous().view(torch.uint8),
                                         codebook.contiguous(), w8, GROUP_SIZE, stream)
    torch.cuda.synchronize()
    # s_channel is per-output-channel [N] and must scale w8 along dim 0. Writing [1,N] here
    # broadcasts along K whenever N == K and silently produces an ~11% mismatch (measured in
    # the lab harness on 2026-09-18; the kernel was never wrong, the test was).
    ref_kernel = (xq.float() * xs) @ (w8.float() * s_channel.view(-1, 1)).t()
    record["kernel_exact_rel_l2"] = rel_l2(out_cuda, ref_kernel)
    record["kernel_exact_max_abs"] = (out_cuda.float() - ref_kernel).abs().max().item()
    print(f"  CUDA kernel vs same-activation reference: relL2={record['kernel_exact_rel_l2']:.6e} "
          f"maxabs={record['kernel_exact_max_abs']:.6e}")
    check(record["kernel_exact_rel_l2"] < 5e-3,
          f"CUDA kernel matches same-activation reference within 5e-3 relL2 "
          f"(got {record['kernel_exact_rel_l2']:.3e})")

    record["cuda_out_all_finite"] = bool(torch.isfinite(out_cuda.float()).all().item())
    check(record["cuda_out_all_finite"], "CUDA output contains no NaN/Inf")

    for _ in range(3):
        w4a8_int8_linear(x, qdata, s_rel, s_channel, codebook=codebook, correction=correction,
                         bias=None, group_size=GROUP_SIZE, convrot_groupsize=CONVROT,
                         out_dtype=torch.bfloat16)
    torch.cuda.synchronize()
    iters = 20
    t0 = time.perf_counter()
    for _ in range(iters):
        w4a8_int8_linear(x, qdata, s_rel, s_channel, codebook=codebook, correction=correction,
                         bias=None, group_size=GROUP_SIZE, convrot_groupsize=CONVROT,
                         out_dtype=torch.bfloat16)
    torch.cuda.synchronize()
    t_cuda = (time.perf_counter() - t0) / iters

    for _ in range(3):
        with registry.use_backend("eager"):
            w4a8_int8_linear(x, qdata, s_rel, s_channel, codebook=codebook, correction=correction,
                             bias=None, group_size=GROUP_SIZE, convrot_groupsize=CONVROT,
                             out_dtype=torch.bfloat16)
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        with registry.use_backend("eager"):
            w4a8_int8_linear(x, qdata, s_rel, s_channel, codebook=codebook, correction=correction,
                             bias=None, group_size=GROUP_SIZE, convrot_groupsize=CONVROT,
                             out_dtype=torch.bfloat16)
    torch.cuda.synchronize()
    t_eager = (time.perf_counter() - t0) / iters

    flops = 2 * M * N * K
    record["timing"] = {
        "shape": {"m": M, "n": N, "k": K},
        "cuda_ms": t_cuda * 1e3,
        "eager_ms": t_eager * 1e3,
        "cuda_tflops": flops / t_cuda / 1e12,
        "eager_tflops": flops / t_eager / 1e12,
        "speedup": t_eager / t_cuda,
    }
    print(f"  CUDA : {t_cuda*1e3:.3f} ms  ({record['timing']['cuda_tflops']:.3f} TFLOP/s)")
    print(f"  eager: {t_eager*1e3:.3f} ms  ({record['timing']['eager_tflops']:.3f} TFLOP/s)")
    print(f"  speedup: {record['timing']['speedup']:.2f}x")
    check(t_cuda > 0 and record["timing"]["cuda_tflops"] > 1.0,
          "CUDA W4A8 GEMM achieves >1 TFLOP/s on this GPU")

    record["cuda_peak_vram_gib"] = round(torch.cuda.max_memory_allocated() / 2**30, 3)
    print(f"  peak torch VRAM during test: {record['cuda_peak_vram_gib']} GiB")

    section("VERDICT")
    record["failures"] = FAILURES
    record["gate"] = "PASS" if not FAILURES else "FAIL"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({"gate": record["gate"], "failures": FAILURES}, indent=2))
    print(f"record -> {out_path}")
    return 0 if not FAILURES else 2


if __name__ == "__main__":
    sys.exit(main())
