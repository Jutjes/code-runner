import tempfile, subprocess, textwrap, os, uuid, json, shutil
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

app = FastAPI(title="Code Runner API", version="1.0.0", description="Run Python code and tests in a sandbox")

MAX_CODE_CHARS = 20000
TIMEOUT_SEC = 5

class RunRequest(BaseModel):
    code: str = Field(..., description="Python source code to run")
    stdin: Optional[str] = Field(default="", description="Optional stdin")
    timeout_sec: Optional[int] = Field(default=TIMEOUT_SEC, ge=1, le=20)

class RunResponse(BaseModel):
    ok: bool
    stdout: str
    stderr: str
    exit_code: int

class TestRequest(BaseModel):
    code: str = Field(..., description="Python module(s) code. Put your main code in file `solution.py`.")
    tests: str = Field(..., description="Pytest tests, e.g. test_solution.py")
    timeout_sec: Optional[int] = Field(default=TIMEOUT_SEC, ge=1, le=20)

class TestResponse(BaseModel):
    ok: bool
    summary: str
    stdout: str
    stderr: str
    exit_code: int

@app.get("/ping")
def ping():
    return {"status": "ok", "message": "pong"}

def _run_cmd(cmd, cwd, timeout, input_str=""):
    try:
        proc = subprocess.run(
            cmd,
            input=input_str.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            timeout=timeout,
            check=False
        )
        return proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout.decode(errors="replace") if e.stdout else "", "TIMEOUT"
    except Exception as e:
        return 1, "", f"ERROR: {e}"

def _limit(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "\n...[truncated]..."

@app.post("/run", response_model=RunResponse)
def run_code(req: RunRequest):
    if len(req.code) > MAX_CODE_CHARS:
        raise HTTPException(status_code=413, detail="Code too large")
    work = tempfile.mkdtemp(prefix="run-")
    try:
        code_path = os.path.join(work, "main.py")
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(req.code)
        rc, out, err = _run_cmd(["python", "main.py"], cwd=work, timeout=req.timeout_sec, input_str=req.stdin or "")
        return RunResponse(ok=(rc == 0), stdout=_limit(out, 100_000), stderr=_limit(err, 100_000), exit_code=rc)
    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/test", response_model=TestResponse)
def run_tests(req: TestRequest):
    if len(req.code) > MAX_CODE_CHARS or len(req.tests) > MAX_CODE_CHARS:
        raise HTTPException(status_code=413, detail="Code or tests too large")
    work = tempfile.mkdtemp(prefix="test-")
    try:
        # write solution and tests
        with open(os.path.join(work, "solution.py"), "w", encoding="utf-8") as f:
            f.write(req.code)
        with open(os.path.join(work, "test_solution.py"), "w", encoding="utf-8") as f:
            f.write(req.tests)

        # install nothing here (deps are baked into image via requirements)
        rc, out, err = _run_cmd(
            ["pytest", "-q", "--maxfail=1", "--disable-warnings"],
            cwd=work,
            timeout=req.timeout_sec
        )

        summary = "tests passed" if rc == 0 else "tests failed"
        return TestResponse(ok=(rc == 0), summary=summary, stdout=_limit(out, 100_000), stderr=_limit(err, 100_000), exit_code=rc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
