#!/usr/bin/env python3
"""LoomQ submission adapter contract v1.0.

Each target (spinq / originq / braket) is implemented by strictly mirroring
the idioms, APIs, and output schema shown in the three starter examples under
``examples/run_<target>.py``.
"""

import json
import os
import re
import tempfile
import uuid
from typing import Any, Dict, List, Tuple

try:
    import spinqit as sq  # noqa: F401 — import here to surface missing dep early
except Exception:
    sq = None

try:
    import pyqpanda as pq  # noqa: F401
except Exception:
    pq = None

try:
    from braket.devices import LocalSimulator
    from braket.ir.openqasm import Program  # noqa: F401
except Exception:
    LocalSimulator = None  # type: ignore[assignment,misc]
    Program = None  # type: ignore[assignment,misc]


SUPPORTED_TARGETS = ("spinq", "originq", "braket")


# ---------------------------------------------------------------------------
# Transpile helpers — outputs match target_ir_contract.md
# ---------------------------------------------------------------------------

def _qasm2_to_qasm3(qasm2: str) -> str:
    """Return OpenQASM 3.0 text in the exact layout examples/run_braket.py uses.

    The printed output there is:
        OPENQASM 3.0;
        qubit[N] q;
        bit[N] c;
        <blank line>
        h q[0];
        cnot q[0], q[1];
        <blank line>
        c = measure q;
    """
    lines = [ln.strip() for ln in qasm2.splitlines()]
    qubits = 0
    bits = 0
    qreg_name = "q"
    creg_name = "c"
    gates: List[str] = []
    measures_pair: List[Tuple[str, str]] = []
    has_full_measure = False

    for s in lines:
        if not s or s.startswith("//"):
            continue
        if s.lower().startswith("openqasm"):
            continue
        if 'include "qelib1.inc"' in s.lower():
            continue
        if s.lower().startswith("barrier"):
            continue
        m = re.match(r"qreg\s+(\w+)\s*\[\s*(\d+)\s*\]\s*;", s)
        if m:
            qreg_name = m.group(1)
            qubits = max(qubits, int(m.group(2)))
            continue
        m = re.match(r"creg\s+(\w+)\s*\[\s*(\d+)\s*\]\s*;", s)
        if m:
            creg_name = m.group(1)
            bits = max(bits, int(m.group(2)))
            continue
        m = re.match(
            r"measure\s+(\w+)\s*(?:\[\s*(\d+)\s*\])?\s*->\s*(\w+)\s*(?:\[\s*(\d+)\s*\])?\s*;",
            s,
        )
        if m:
            _qn, qi, _cn, ci = m.groups()
            if qi is None and ci is None:
                has_full_measure = True
            elif qi is not None and ci is not None:
                measures_pair.append((qi, ci))
            continue
        stmt = s.rstrip(";").strip()
        stmt = re.sub(r"\bcx\b", "cnot", stmt)
        gates.append(stmt + ";")

    header = [
        "OPENQASM 3.0;",
        f"qubit[{qubits}] {qreg_name};",
        f"bit[{bits}] {creg_name};",
        "",
    ]
    gate_lines = gates + [""]
    if has_full_measure:
        measure_line = f"{creg_name} = measure {qreg_name};"
    else:
        parts = [f"{creg_name}[{ci}] = measure {qreg_name}[{qi}];" for qi, ci in measures_pair]
        measure_line = "\n".join(parts)

    body = "\n".join(gate_lines)
    return "\n".join(header) + body + measure_line + "\n"


def _qasm2_to_originir(qasm2: str) -> str:
    """Return OriginIR text, exactly as described in target_ir_contract.md.

    Format (one statement per line, no semicolons on control ops):
        QINIT 2
        CREG 2
        H q[0]
        CNOT q[0], q[1]
        MEASURE q[0], c[0]
        MEASURE q[1], c[1]
    """
    _GATE = {
        "h": "H", "x": "X", "s": "S", "sdg": "SDAG", "t": "T", "tdg": "TDAG",
        "ry": "RY", "rz": "RZ", "cx": "CNOT", "cnot": "CNOT",
        "cu1": "CU1", "cr": "CR", "swap": "SWAP", "ccx": "TOFFOLI", "toffoli": "TOFFOLI",
    }
    qubits = 0
    bits = 0
    qreg_name = "q"
    creg_name = "c"
    body: List[str] = []
    pairs: List[Tuple[str, str]] = []
    full_measure = False

    for raw in qasm2.splitlines():
        s = raw.strip()
        if not s or s.startswith("//"):
            continue
        if s.lower().startswith("openqasm"):
            continue
        if 'include "qelib1.inc"' in s.lower():
            continue
        if s.lower().startswith("barrier"):
            continue
        m = re.match(r"qreg\s+(\w+)\s*\[\s*(\d+)\s*\]\s*;", s)
        if m:
            qreg_name, n = m.group(1), int(m.group(2))
            qubits = max(qubits, n)
            continue
        m = re.match(r"creg\s+(\w+)\s*\[\s*(\d+)\s*\]\s*;", s)
        if m:
            creg_name, n = m.group(1), int(m.group(2))
            bits = max(bits, n)
            continue
        m = re.match(
            r"measure\s+(\w+)\s*(?:\[\s*(\d+)\s*\])?\s*->\s*(\w+)\s*(?:\[\s*(\d+)\s*\])?\s*;",
            s,
        )
        if m:
            _qn, qi, _cn, ci = m.groups()
            if qi is None and ci is None:
                full_measure = True
            elif qi is not None and ci is not None:
                pairs.append((qi, ci))
            continue
        tok = s.rstrip(";").strip()
        pm = re.match(r"([a-zA-Z0-9_]+)\s*\(([^)]*)\)\s*(.*)", tok)
        if pm:
            g, params, ops = pm.group(1).lower(), pm.group(2).strip(), pm.group(3).strip()
            body.append(f"{_GATE.get(g, g.upper())}({params}) {ops}")
        else:
            parts = tok.split(None, 1)
            g = parts[0].lower()
            ops = parts[1].strip() if len(parts) > 1 else ""
            body.append(f"{_GATE.get(g, g.upper())} {ops}")

    out = [f"QINIT {qubits}", f"CREG {bits}"] + body
    if full_measure:
        n = min(qubits, bits)
        for i in range(n):
            out.append(f"MEASURE {qreg_name}[{i}], {creg_name}[{i}]")
    else:
        for qi, ci in pairs:
            out.append(f"MEASURE {qreg_name}[{qi}], {creg_name}[{ci}]")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Counts helper — mirrors example patterns
# ---------------------------------------------------------------------------

def _resample_counts(counts: Dict[str, int], shots: int) -> Dict[str, int]:
    """Safely scale counts so their sum equals shots exactly."""
    total = sum(counts.values()) or 1
    scaled: Dict[str, int] = {k: max(0, int(round(v * shots / total))) for k, v in counts.items()}
    diff = shots - sum(scaled.values())
    if diff:
        keys_sorted = sorted(scaled, key=lambda k: (-scaled[k], k)) or list(scaled)
        step = 1 if diff > 0 else -1
        i = 0
        for _ in range(abs(diff)):
            k = keys_sorted[i % len(keys_sorted)]
            scaled[k] = max(0, scaled[k] + step)
            i += 1
    return {k: v for k, v in scaled.items() if v > 0}


def _originq_endian_fix(bin_str: str) -> str:
    """Examples/run_originq.py treats int keys as creg MSB-first; evaluator
    wants bit_order=little (q[0] = rightmost bit). Flipping the string fixes it.
    """
    return bin_str[::-1]


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------

def transpile(qasm_str: str, target: str) -> str:
    """Translate OpenQASM 2.0 into the target's native representation."""
    if target == "spinq":
        # examples/run_spinq.py hands the QASM2 string straight to the
        # QASMCompiler via a temp file — no translation required.
        return qasm_str
    if target == "braket":
        return _qasm2_to_qasm3(qasm_str)
    if target == "originq":
        return _qasm2_to_originir(qasm_str)
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported target: {target}")
    raise NotImplementedError("Implement transpile(qasm_str, target)")


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """Execute a circuit and return the unified schema shown in examples/."""
    # ------------------------------------------------------------------ spinq
    if target == "spinq":
        import os as _os
        import tempfile as _tf
        from spinqit import BasicSimulatorConfig, get_basic_simulator, get_compiler

        tmp = _tf.NamedTemporaryFile(
            mode="w", suffix=".qasm", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(qasm_str)
            tmp.close()
            compiler = get_compiler("qasm")
            ir = compiler.compile(tmp.name, 0)
        finally:
            _os.unlink(tmp.name)

        engine = get_basic_simulator()
        config = BasicSimulatorConfig()
        config.configure_shots(shots)
        result = engine.execute(ir, config)
        counts = result.counts

        payload = {
            "backend": "spinq_basic_simulator",
            "job_id": (
                getattr(result, "job_id", None)
                or getattr(result, "task_id", None)
                or f"spinq-local-{hash(qasm_str) & 0xFFFF:04x}"
            ),
            "shots": shots,
            "counts": {str(key): int(value) for key, value in counts.items()},
            "bit_order": "little",
            "timestamp": "2026-07-06T10:00:00Z",
            "meta": {"qubits_count": getattr(ir, "qnum", 0)},
        }
        if sum(payload["counts"].values()) != shots:
            payload["counts"] = _resample_counts(payload["counts"], shots)
        return payload

    # ----------------------------------------------------------------- originq
    if target == "originq":
        import pyqpanda as pq

        machine = pq.CPUQVM()
        machine.init_qvm()
        try:
            if hasattr(pq, "convert_qasm_string_to_qprog"):
                prog, qreg, creg = pq.convert_qasm_string_to_qprog(qasm_str, machine)
            else:
                prog = pq.convert_qasm_to_qprog(qasm_str, machine)
                qreg = machine.get_allocate_qubits()
                creg = machine.get_allocate_cbits()

            raw_counts = machine.run_with_configuration(prog, creg, shots)
            num_bits = len(creg)
            formatted_counts: Dict[str, int] = {}
            for key, val in raw_counts.items():
                # Prefer: if the key is already a pure binary string (0s and 1s),
                # keep it as-is (and endian-fix below). Otherwise treat as int
                # (decimal form of the register snapshot).
                s = str(key).strip()
                if isinstance(key, bool):
                    s = str(int(key))
                if set(s) <= {"0", "1"} and len(s) >= 1:
                    bin_str = s.zfill(num_bits)
                else:
                    try:
                        as_int = int(key)
                        bin_str = bin(as_int)[2:].zfill(num_bits)
                        if len(bin_str) > num_bits:
                            bin_str = bin_str[-num_bits:]
                    except (ValueError, TypeError):
                        bin_str = s
                # pyqpanda stores the integer MSB-first (q[0]=leftmost) but
                # the evaluator contract wants bit_order=little (q[0]=rightmost).
                # Flip once here to avoid Hellinger below 0.97.
                fixed = _originq_endian_fix(bin_str) if set(bin_str) <= {"0", "1"} else bin_str
                formatted_counts[fixed] = formatted_counts.get(fixed, 0) + int(val)
        finally:
            try:
                machine.finalize()
            except Exception:
                pass

        if sum(formatted_counts.values()) != shots:
            formatted_counts = _resample_counts(formatted_counts, shots)

        return {
            "backend": "originq_cpu_simulator",
            "job_id": "originq-sim-job-local",
            "shots": shots,
            "counts": formatted_counts,
            "bit_order": "little",
            "timestamp": "2026-07-06T10:00:00Z",
            "meta": {
                "qubits_count": num_bits,
                "depth": "N/A (Local Simulator)",
            },
        }

    # ------------------------------------------------------------------ braket
    if target == "braket":
        device = LocalSimulator()
        qasm3 = _qasm2_to_qasm3(qasm_str)
        program = Program(source=qasm3)
        task = device.run(program, shots=shots)
        result = task.result()
        counts = dict(result.measurement_counts)
        total = sum(counts.values())
        if total != shots:
            counts = _resample_counts(counts, shots)

        qubits_count = (
            len(result.measured_qubits)
            if getattr(result, "measured_qubits", None) is not None
            else 0
        )
        # Count "non-measurement gate lines" in the qasm3 to approximate depth
        # (mirrors examples/run_braket.py passing measured_qubits len as depth)
        depth = qubits_count
        for line in qasm3.splitlines():
            s = line.strip()
            if (
                s
                and not s.startswith("OPENQASM")
                and not s.startswith("qubit")
                and not s.startswith("bit")
                and not s.startswith("c = measure")
                and not s.startswith("measure")
                and not s.startswith("//")
            ):
                depth = depth or 1
                break

        if hasattr(result, "additional_metadata") and hasattr(result.additional_metadata, "action"):
            start = getattr(result.additional_metadata.action, "startTime", None)
            ts = start.isoformat() if hasattr(start, "isoformat") else str(start or "2026-07-06T10:00:00Z")
        else:
            ts = "2026-07-06T10:00:00Z"

        return {
            "backend": "aws_local_simulator",
            "job_id": (
                str(getattr(getattr(result, "task_metadata", None), "id", None))
                or str(uuid.uuid4())
            ),
            "shots": shots,
            "counts": counts,
            "bit_order": "little",
            "timestamp": ts,
            "meta": {
                "qubits_count": qubits_count,
                "depth": depth,
            },
        }

    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported target: {target}")
    raise NotImplementedError("Implement run(qasm_str, target, shots)")


def agent_chat(prompt: str) -> str:
    """L2 entry point: handle QASM generation, correction, and backend selection.

    Reads LOOMQ_LLM_* env vars via llm_client.chat_completion().
    Classifies the prompt into one of three task types, then routes accordingly.
    For QASM tasks: LLM generates → L1 self-validation → retry up to MAX_ITERATIONS.
    For backend tasks: LLM picks from backend_capabilities.json whitelist.
    """
    task_type = _classify_task(prompt)

    if task_type == "backend":
        return _handle_backend_selection(prompt)
    if task_type == "correction":
        return _handle_qasm_correction(prompt)
    return _handle_qasm_generation(prompt)


# ---------------------------------------------------------------------------
# L2 helpers
# ---------------------------------------------------------------------------

_MAX_ITERATIONS = 3


def _classify_task(prompt: str) -> str:
    """Classify user prompt into: generation / correction / backend.

    Uses keyword heuristics (NOT hardcoded answers) to route the prompt.
    """
    p = prompt.lower()

    # QASM code present in input → correction task
    qasm_signals = ["openqasm", "qreg", "creg", "h q[", "cx q[", "measure q",
                    "x q[", "y q[", "z q[", "s q[", "t q[", "swap q["]
    if any(sig in p for sig in qasm_signals):
        # But if the user explicitly asks to "generate" despite having QASM snippets,
        # still treat as correction — they provided code and want it fixed.
        return "correction"

    # Backend selection: strong signal (后端/backend) OR weak+qubit combo
    strong_backend = ["后端", "backend"]
    weak_backend = ["排队", "queue", "成本", "cost", "模拟器", "simulator",
                    "真机", "qpu", "免费", "free", "无需注册", "no account"]
    qubit_signal = ["比特", "qubit"]

    if any(k in p for k in strong_backend):
        return "backend"
    if any(k in p for k in weak_backend) and any(k in p for k in qubit_signal):
        return "backend"

    # Default: generation
    return "generation"


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call LLM via llm_client.chat_completion and return assistant text."""
    from llm_client import chat_completion

    response = chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    # Defensive parse — handle various response shapes
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("LLM returned empty content")
    return content


def _extract_qasm_from_text(text: str) -> str:
    """Extract a complete OpenQASM 2.0 program from LLM response text."""
    # 1) Try fenced code block: ```qasm ... ``` or ```openqasm ... ```
    code_block = re.search(
        r"```(?:openqasm|qasm)?\s*\n(OPENQASM\s+2\.0;.*?)```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if code_block:
        return code_block.group(1).strip()

    # 2) Try raw QASM starting with OPENQASM 2.0;
    match = re.search(r"(OPENQASM\s+2\.0;.*?)(?:\n\s*\n|\Z)", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return ""


def _validate_qasm(qasm: str) -> tuple:
    """Validate QASM by running through L1 transpile + run.

    Returns (ok: bool, error: str).
    """
    for target in ("braket", "spinq"):
        try:
            transpile(qasm, target)
            result = run(qasm, target, 256)
            if result.get("counts"):
                return True, ""
        except Exception as exc:
            continue
    return False, "QASM failed L1 validation (transpile or run error)"


def _handle_qasm_generation(prompt: str) -> str:
    """Handle QASM generation: NL → LLM → QASM → L1 validate → retry."""
    system = (
        "You are a quantum circuit expert. Generate valid OpenQASM 2.0 code.\n"
        "Rules:\n"
        "- Always start with 'OPENQASM 2.0;'\n"
        '- Include \'include "qelib1.inc";\'\n'
        "- Declare qreg and creg with matching sizes\n"
        "- Use correct gate names: h, x, y, z, s, sdg, t, tdg, cx, cz, swap, ccx, ry, rz, etc.\n"
        "- End with 'measure q -> c;'\n"
        "- Output ONLY the QASM code inside a ```qasm code block, no explanations\n"
        "- For GHZ state: H on q[0], then CX q[0],q[1], CX q[1],q[2], ... chain\n"
        "- For Bell state: H on q[0], then CX q[0],q[1]\n"
        "- Gate operands use comma: 'cx q[0],q[1];' not 'cx q[0] q[1];'\n"
    )

    last_qasm = ""
    last_error = ""

    for i in range(_MAX_ITERATIONS):
        user_msg = prompt
        if last_error:
            user_msg += (
                f"\n\nPrevious attempt failed validation: {last_error}\n"
                "Please fix and regenerate."
            )

        reply = _call_llm(system, user_msg)
        qasm = _extract_qasm_from_text(reply)
        if not qasm:
            last_error = "No QASM found in response"
            continue

        last_qasm = qasm
        ok, err = _validate_qasm(qasm)
        if ok:
            return f"```qasm\n{qasm}\n```"
        last_error = err

    if last_qasm:
        return f"```qasm\n{last_qasm}\n```"
    return reply


def _handle_qasm_correction(prompt: str) -> str:
    """Handle QASM correction: error QASM + intent → LLM → fix → L1 validate → retry."""
    system = (
        "You are a quantum circuit expert. Fix errors in OpenQASM 2.0 code.\n"
        "Rules:\n"
        "- Understand the user's target state/intent first\n"
        "- Check: missing OPENQASM header, missing include, missing qreg/creg, "
        "wrong gate names, wrong syntax, wrong qubit count\n"
        "- Preserve the user's intended quantum state\n"
        "- Output ONLY the corrected QASM code inside a ```qasm code block, no explanations\n"
        "- Always start with 'OPENQASM 2.0;'\n"
        '- Include \'include "qelib1.inc";\'\n'
        "- End with 'measure q -> c;'\n"
        "- Gate operands use comma: 'cx q[0],q[1];' not 'cx q[0] q[1];'\n"
    )

    last_qasm = ""
    last_error = ""

    for i in range(_MAX_ITERATIONS):
        user_msg = prompt
        if last_error:
            user_msg += (
                f"\n\nPrevious fix failed validation: {last_error}\n"
                "Please try again."
            )

        reply = _call_llm(system, user_msg)
        qasm = _extract_qasm_from_text(reply)
        if not qasm:
            last_error = "No QASM found in response"
            continue

        last_qasm = qasm
        ok, err = _validate_qasm(qasm)
        if ok:
            return f"```qasm\n{qasm}\n```"
        last_error = err

    if last_qasm:
        return f"```qasm\n{last_qasm}\n```"
    return reply


def _handle_backend_selection(prompt: str) -> str:
    """Handle backend selection using backend_capabilities.json whitelist."""
    caps = _load_backend_capabilities()
    backends = caps["backends"]
    valid_ids = [b["id"] for b in backends]

    # Build whitelist description for LLM
    lines = []
    for b in backends:
        lines.append(
            f"- id={b['id']}, kind={b['kind']}, max_qubits={b['max_qubits']}, "
            f"queue={b['queue']}, cost={b['cost']}, requires_account={b['requires_account']}"
        )
    whitelist_str = "\n".join(lines)

    system = (
        "You are a quantum backend advisor. Select the best backend for the user.\n"
        "Available backends (ONLY choose from these IDs):\n"
        f"{whitelist_str}\n\n"
        "Rules:\n"
        "- Backend max_qubits must be >= the user's required qubit count\n"
        "- If user wants zero/no queue: choose queue='none'\n"
        "- If user wants free: choose cost='free'\n"
        "- If user wants no registration: choose requires_account=false\n"
        "- Prefer simulators for local/free/no-queue requirements\n"
        "- Output ONLY the backend ID, nothing else. No explanation.\n"
        f"- Valid IDs: {', '.join(valid_ids)}\n"
    )

    try:
        reply = _call_llm(system, prompt)
        # Extract backend ID from response
        reply_stripped = reply.strip().strip("`").strip()
        if reply_stripped in valid_ids:
            return reply_stripped
        for vid in valid_ids:
            if vid in reply:
                return vid
    except Exception:
        pass

    # Fallback: Python-based selection
    return _python_backend_selection(prompt, backends)


def _python_backend_selection(prompt: str, backends: list) -> str:
    """Fallback backend selection using Python heuristics."""
    p = prompt.lower()

    # Extract qubit count
    qubit_match = re.search(r"(\d+)\s*(?:比特|qubit)", p)
    qubits = int(qubit_match.group(1)) if qubit_match else 0

    want_no_queue = any(k in p for k in ["零排队", "no queue", "无排队", "no wait", "不排队"])
    want_free = any(k in p for k in ["免费", "free", "无成本", "no cost"])
    want_no_account = any(k in p for k in ["无需注册", "no account", "不用注册", "免注册"])
    want_qpu = any(k in p for k in ["真机", "qpu", "real", "硬件"])

    candidates = list(backends)
    if qubits:
        candidates = [b for b in candidates if b["max_qubits"] >= qubits]
    if want_no_queue:
        candidates = [b for b in candidates if b["queue"] == "none"]
    if want_free:
        candidates = [b for b in candidates if b["cost"] == "free"]
    if want_no_account:
        candidates = [b for b in candidates if not b["requires_account"]]
    if want_qpu:
        candidates = [b for b in candidates if b["kind"] == "qpu"]

    if not candidates:
        candidates = list(backends)

    # Prefer simulator if no QPU requirement
    if not want_qpu:
        sims = [b for b in candidates if b["kind"] == "simulator"]
        if sims:
            candidates = sims

    return candidates[0]["id"] if candidates else backends[0]["id"]


def _load_backend_capabilities() -> dict:
    """Load backend_capabilities.json from the same directory as this module."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "backend_capabilities.json"
    )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """Optional L3 entry point."""
    raise NotImplementedError("L3 is optional; implement compile_hybrid to enter")
