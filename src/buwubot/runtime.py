"""Process lifetime helpers; locks are released by the OS even after a crash."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

import httpx
import psutil


class InstanceLock:
    def __init__(self, path: Path, *, port: int = 8765):
        self.path = path
        self.port = port
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, os.SEEK_END) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("BuwuBot is already running") from exc
        try:
            identity = {"pid":os.getpid(),"created_at":psutil.Process().create_time(),
                        "executable":psutil.Process().exe(),"port":self.port}
            temporary = self.path.with_suffix(".instance.tmp")
            temporary.write_text(json.dumps(identity),encoding="utf-8")
            temporary.replace(self.path.with_suffix(".instance.json"))
        except BaseException:
            self.file.close()
            self.file = None
            raise
        return self

    def __exit__(self, *args):
        if self.file:
            try:
                self.path.with_suffix(".instance.json").unlink(missing_ok=True)
            finally:
                self.file.close()
                self.file = None


def _managed_process(directory: Path):
    directory = directory.resolve()
    try:
        identity = json.loads((directory/"bot.instance.json").read_text(encoding="utf-8"))
        process = psutil.Process(int(identity["pid"]))
        if abs(process.create_time()-float(identity["created_at"])) > .01:
            return None,identity
        command = process.cmdline()
        if (Path(process.exe()).resolve()!=Path(identity["executable"]).resolve()
                or "buwubot.app" not in command):
            raise RuntimeError("process identity mismatch; refusing to stop it")
        data = Path(command[command.index("--data-dir")+1]) if "--data-dir" in command else Path("data")
        if not data.is_absolute():
            data = Path(process.cwd())/data
        if data.resolve()!=directory:
            raise RuntimeError("process data-directory identity mismatch")
        return process,identity
    except (FileNotFoundError,psutil.NoSuchProcess):
        return None,{}
    except (ValueError,KeyError,IndexError,psutil.AccessDenied) as exc:
        raise RuntimeError("cannot verify process identity") from exc


def inspect_instance(directory: Path) -> dict:
    process,identity = _managed_process(directory)
    result = {"running":process is not None,"pid":process.pid if process else None,
              "port":identity.get("port",8765),"reachable":False}
    if process:
        try:
            port = int(result["port"])
            if not 1<=port<=65535:
                raise RuntimeError("invalid service port in process identity")
            with httpx.Client(trust_env=False,timeout=1) as http:
                response = http.get(f"http://127.0.0.1:{port}/api/status")
                result["reachable"] = response.status_code==200 and response.json().get("instance_pid")==process.pid
        except (httpx.HTTPError,ValueError):
            pass
    return result


def stop_instance(directory: Path, *, force: bool = False):
    process,identity = _managed_process(directory)
    directory.mkdir(parents=True,exist_ok=True)
    (directory/"stop.request").write_text("Explicitly stopped by the administrator.",encoding="utf-8")
    if not process:
        return
    port = int(identity["port"])
    if not 1<=port<=65535:
        raise RuntimeError("invalid service port in process identity")
    try:
        with httpx.Client(trust_env=False,timeout=3) as http:
            status=http.get(f"http://127.0.0.1:{port}/api/status")
            status.raise_for_status()
            if status.json().get("instance_pid")!=process.pid:
                raise RuntimeError("service port belongs to a different process")
            response = http.post(f"http://127.0.0.1:{port}/api/runtime/stop",json={})
            response.raise_for_status()
        process.wait(timeout=10)
        return
    except (httpx.HTTPError,psutil.TimeoutExpired):
        if not force:
            raise RuntimeError("graceful stop failed; inspect logs or explicitly use --force") from None
    # Force is opt-in, after verifying PID, creation time, executable, module and data path.
    children = process.children(recursive=True)
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        process.terminate()
        process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


def configured_qq_provider(directory: Path) -> str:
    database = directory / "bot.db"
    if not database.exists():
        return "napcat"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
    if not row:
        raise RuntimeError("QQ provider configuration is missing")
    provider = json.loads(row[0]).get("qq_provider", "napcat")
    if provider not in {"napcat", "snowluma"}:
        raise RuntimeError("QQ provider configuration is invalid")
    return provider


def main():
    parser = argparse.ArgumentParser(description="Inspect or stop a verified BuwuBot process")
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status",action="store_true")
    actions.add_argument("--stop",action="store_true")
    actions.add_argument("--qq-provider", action="store_true")
    parser.add_argument("--force",action="store_true")
    args = parser.parse_args()
    if args.stop:
        stop_instance(args.data_dir,force=args.force)
    elif args.qq_provider:
        print(configured_qq_provider(args.data_dir))
    else:
        result = inspect_instance(args.data_dir)
        print(json.dumps(result))
        raise SystemExit(0 if result["running"] and result["reachable"] else 1)


if __name__=="__main__":
    main()
