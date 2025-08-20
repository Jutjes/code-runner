# app.py
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, Field

app = FastAPI(
    title="Code Runner API",
    version="1.0.0",
    description="Run Python code & pytest tests in a lightweight sandbox."
)

# ---------- Config ----------
MAX_CODE_CHARS = 20_000
TIMEOUT_SEC_DEFAULT = 5
API_KEY_ENV = "RUNNER_API_KEY"  # zet deze als Railway variable
OUTPUT_LIMIT = 100_000          # max chars terug in stdout/stderr


# ---------- Security ----------
def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    expected = os.getenv(API_KEY_ENV, "")
    if not expected:
        # Server is bedoeld om met key te draaien; zonder key = misconfig
        raise HTTPException(status_code=503, detail="API not configured")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------- Models ----------
class RunRequest(BaseModel):
    code: str = Field(..., description="Python source code to run")
    stdin: Optional[str] = Field(default="", description="Optional stdin")
    timeout_sec: Optional[int] = Field(default=TIMEOUT_SEC_DEFAULT, ge=1, le=20)


class RunResponse(BaseModel):
    ok: bool
    stdout: str
    stderr: str
    exit_code: int


class TestRequest(BaseModel):
    code: str = Field(..., description="Content for solution.py")
    tests: str = Field(..., description="Pytest tests (e.g. test_solution.py)")
    timeout_sec: Optional[int] = Field(default=TIMEOUT_SEC_DEFAULT, ge=1, le=20)


class TestResponse(BaseModel):
    ok: bool
    summary: str
    stdout: str
    stderr: str
    exit_code: int


# ---------- Helpers ----------
def _limit(s: str, n: int) -> str:
    if s is None:
        return ""
    return s if len(s) <= n else s[:n] + "\n...[truncated]..."

def _run_cmd(cmd, cwd, timeout, input_str=""):
    # Minimal sandbox via subprocess; echte isolatie komt van container runtime
    env = os.environ.copy()
    # Optioneel: verwijder proxy vars om ongewenst netwerkgedrag te minimaliseren
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        env.pop(k, None)

    try:
        proc = subprocess.run(
            cmd,
            input=(input_str or "").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            timeout=timeout,
            check=False,
            env=env
        )
        return proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace")
        err = "TIMEOUT"
        return 124, out, err
    except Exception as e:
        return 1, "", f"ERROR: {e}"


# ---------- Endpoints ----------
@app.get("/ping")
def ping():
    # Laat /ping open voor simpele health checks
    return {"status": "ok", "message": "pong"}

@app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_key)])
def run_code(req: RunRequest):
    if len(req.code) > MAX_CODE_CHARS:
        raise HTTPException(status_code=413, detail="Code too large")

    work = tempfile.mkdtemp(prefix="run-")
    try:
        code_path = os.path.join(work, "main.py")
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(req.code)

        rc, out, err = _run_cmd(
            ["python", "main.py"],
            cwd=work,
            timeout=req.timeout_sec or TIMEOUT_SEC_DEFAULT,
            input_str=req.stdin or ""
        )

        return RunResponse(
            ok=(rc == 0),
            stdout=_limit(out, OUTPUT_LIMIT),
            stderr=_limit(err, OUTPUT_LIMIT),
            exit_code=rc
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/test", response_model=TestResponse, dependencies=[Depends(require_api_key)])
def run_tests(req: TestRequest):
    if len(req.code) > MAX_CODE_CHARS or len(req.tests) > MAX_CODE_CHARS:
        raise HTTPException(status_code=413, detail="Code or tests too large")

    work = tempfile.mkdtemp(prefix="test-")
    try:
        # Schrijf solution + tests
        with open(os.path.join(work, "solution.py"), "w", encoding="utf-8") as f:
            f.write(req.code)
        with open(os.path.join(work, "test_solution.py"), "w", encoding="utf-8") as f:
            f.write(req.tests)

        # Pytest draaien
        rc, out, err = _run_cmd(
            ["pytest", "-q", "--maxfail=1", "--disable-warnings"],
            cwd=work,
            timeout=req.timeout_sec or TIMEOUT_SEC_DEFAULT
        )

        summary = "tests passed" if rc == 0 else "tests failed"
        return TestResponse(
            ok=(rc == 0),
            summary=summary,
            stdout=_limit(out, OUTPUT_LIMIT),
            stderr=_limit(err, OUTPUT_LIMIT),
            exit_code=rc
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
