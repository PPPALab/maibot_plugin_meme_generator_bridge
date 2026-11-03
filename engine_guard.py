"""Local stdlib-only helper source for macOS/Windows (never downloaded code)."""

# A gated helper lets Windows assign a kill-on-close Job before engine spawn.
# stdin EOF is a Runner-liveness signal, independent of PID reuse. POSIX children
# also inherit the installer lock and stay in the helper's own process group.
GUARD = r"""
import json, os, signal, subprocess, sys, threading

if sys.stdin.buffer.readline() != b"start\n":
    raise SystemExit(122)
lock = int(sys.argv[1])
options = {}
if os.name == "nt":
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": [lock]}
    options.update(startupinfo=startup, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
else:
    options["pass_fds"] = (lock,)
def pump(source, destination):
    try:
        while True:
            data = os.read(source.fileno(), 4096)
            if not data:
                break
            destination.write(data)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass

stop = threading.Event()
def watch_parent():
    sys.stdin.buffer.read(1)
    stop.set()

threads = []
child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, **options)
try:
    # Ownership begins at Popen, before PID output or any helper thread starts.
    # A disappearing Runner can break the handshake pipe at exactly this point.
    print(json.dumps({"pid": child.pid}), flush=True)
    threading.Thread(target=watch_parent, daemon=True).start()
    for source, destination in ((child.stdout, sys.stdout.buffer), (child.stderr, sys.stderr.buffer)):
        thread = threading.Thread(target=pump, args=(source, destination), daemon=True)
        thread.start()
        threads.append(thread)
    while child.poll() is None and not stop.wait(0.1):
        pass
finally:
    if child.poll() is None:
        try:
            if os.name == "nt":
                child.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                child.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=1)
raise SystemExit(child.returncode or 0)
"""
