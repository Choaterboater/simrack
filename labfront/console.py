"""Talk to a sandbox vJunos over its QEMU serial console.

Proxmox gives every switch a ``serial0=socket``: a Unix socket on the host
(``/var/run/qemu-server/<vmid>.serial0``) that carries the switch's console.
``adopt`` uses it to do what an engineer would type by hand to join a fresh
switch to a Mist site: log in as root, set the sandbox root password, turn on
DHCP on fxp0 and paste the outbound-ssh lines Mist hands out.

Nothing here ever puts console output, a command or a password into an error:
the Mist lines carry a device secret, and errors end up in the browser.
"""

from __future__ import annotations

import codecs
import re
import socket
import time

from .errors import BackendError

LOGIN = re.compile(r"(?:^|\n)login:[ \t]*\Z")
PASSWORD = re.compile(r"(?:^|\n)Password:[ \t]*\Z")
INCORRECT = re.compile(r"Login incorrect")
#: The FreeBSD shell root lands in: ``root@sw:~ # `` (or ``root@% `` on older Junos).
SHELL = re.compile(r"(?:^|\n)\S*(?::~[ \t]*#|[@:]\S*%)[ \t]*\Z")
CLI = re.compile(r"(?:^|\n)[\w@.-]+>[ \t]*\Z")
CONFIG = re.compile(r"\[edit[^\]\n]*\][ \t]*\n+[\w@.-]*#[ \t]*\Z")
NEW_PASSWORD = re.compile(r"(?:^|\n)New password:[ \t]*\Z")
RETYPE = re.compile(r"Retype new password:[ \t]*\Z")
DISCARD = re.compile(r"\[yes,no\][ \t]*\(\w+\)[ \t]*\Z")
INET = re.compile(r"inet\s+(\d+(?:\.\d+){3})/\d+")
ERROR = re.compile(r"(?m)^\s*(?:syntax error|unknown command|missing argument|invalid |error:)")

#: Held back until the next read: a CR that may be half of CRLF, a partial escape.
_CARRY = re.compile(r"(?:\r|\x1b(?:\[[0-9;?]*|[()])?)\Z")
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|[()][0-9A-Za-z]|[=>78DEHMc])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: Lines Mist may wrap its set commands in; adopt() handles configure and commit itself.
_WRAPPER = re.compile(r"^(?:configure(?:\s+\w+)?|commit(?:\s.*)?|exit|quit|top)$")

_KEEP = 32 * 1024
_LIMIT = 64 * 1024


class Session:
    """Line discipline over a port: clean text in, prompts matched, CR out.

    A port has ``read(timeout) -> str`` ("" when nothing arrived in time),
    ``send(str)``, ``clock() -> float`` and ``sleep(seconds)``.
    """

    def __init__(self, port) -> None:
        self.port = port
        self.buffer = ""
        self._raw = ""

    def _feed(self, text: str) -> None:
        raw = self._raw + text
        held = _CARRY.search(raw)
        if held:
            raw, self._raw = raw[: held.start()], raw[held.start():]
        else:
            self._raw = ""
        raw = _ANSI.sub("", raw)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        self.buffer += _CONTROL.sub("", raw)
        if len(self.buffer) > _LIMIT:
            self.buffer = self.buffer[-_KEEP:]

    def expect(self, patterns, timeout: float) -> tuple[int, str]:
        """Wait for the first of ``patterns``; return (index, text up to the match).

        The earliest match in the output wins (ties go to the earlier pattern)
        and anything after it stays for the next call. On timeout the result is
        (-1, everything that arrived) and the buffer is emptied.
        """
        compiled = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        deadline = self.port.clock() + timeout
        while True:
            best = None
            for index, pattern in enumerate(compiled):
                found = pattern.search(self.buffer)
                if found and (best is None or found.start() < best[1].start()):
                    best = (index, found)
            if best:
                end = best[1].end()
                text, self.buffer = self.buffer[:end], self.buffer[end:]
                return best[0], text
            remaining = deadline - self.port.clock()
            if remaining <= 0:
                text, self.buffer = self.buffer, ""
                return -1, text
            self._feed(self.port.read(min(remaining, 1.0)))

    def drain(self, quiet: float = 0.5, limit: float = 3.0) -> str:
        """Swallow whatever the console is already saying (boot noise, an old prompt)."""
        start = self.port.clock()
        while True:
            chunk = self.port.read(quiet)
            if not chunk:
                break
            self._feed(chunk)
            if self.port.clock() - start >= limit:
                break
        text, self.buffer = self.buffer, ""
        return text

    def send(self, line: str = "") -> None:
        self.port.send(line + "\r")

    def interrupt(self) -> None:
        self.port.send("\x03")


class SerialConsole:
    """A guest's QEMU serial socket. One client at a time: close ``qm terminal`` first."""

    def __init__(self, path: str, *, connect_timeout: float = 5.0, chunk: int = 128, gap: float = 0.05) -> None:
        self.path = path
        self.chunk = chunk
        self.gap = gap
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.settimeout(connect_timeout)
            self._sock.connect(path)
        except OSError as error:
            self._sock.close()
            raise BackendError(
                "Could not open the switch's serial console.",
                detail=f"{path}: {error.strerror or error}. The switch must be running with serial0=socket, "
                "and the console takes one client at a time: close any open qm terminal first.",
            ) from None

    def read(self, timeout: float) -> str:
        try:
            self._sock.settimeout(max(timeout, 0.01))
            data = self._sock.recv(4096)
        except socket.timeout:
            return ""
        except OSError as error:
            raise BackendError("The serial console failed.", detail=str(error.strerror or error)) from None
        if not data:
            raise BackendError("The serial console closed.", detail="The switch stopped, or another client took its console.")
        return self._decoder.decode(data)

    def send(self, data: str) -> None:
        """Paced, so a long line is not dropped by the guest's tiny UART buffer."""
        raw = data.encode("utf-8")
        try:
            for offset in range(0, len(raw), self.chunk):
                if offset:
                    time.sleep(self.gap)
                self._sock.sendall(raw[offset : offset + self.chunk])
        except OSError as error:
            raise BackendError("Could not write to the serial console.", detail=str(error.strerror or error)) from None

    def clock(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "SerialConsole":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def mist_lines(cmd: str) -> list[str]:
    """The set lines in Mist's outbound-ssh command, without comments or wrappers."""
    lines = []
    for raw in (cmd or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _WRAPPER.match(line):
            continue
        lines.append(line)
    return lines


class _Refused(Exception):
    def __init__(self, label: str, *, at_prompt: bool = True, silent: bool = False) -> None:
        super().__init__(label)
        self.label = label
        self.at_prompt = at_prompt
        self.silent = silent


def _after_echo(text: str) -> str:
    return text.split("\n", 1)[1] if "\n" in text else ""


class _Adoption:
    PROBE = [LOGIN, SHELL, CLI, CONFIG, INCORRECT, PASSWORD]

    def __init__(self, port, host: str, password: str, lines: list[str], *, tries: int, wait: float) -> None:
        self.port = port
        self.session = Session(port)
        self.host = host
        self.password = password
        self.lines = lines
        self.tries = tries
        self.wait = wait
        self.logged_in = False
        self.entered_cli = False
        self.stuck = False

    def run(self) -> dict:
        self.session.drain()
        where = self._probe()
        try:
            if where == 0:
                where = self._login()
            if where == 1:
                self.session.send("cli")
                index, _ = self.session.expect([CLI], 30)
                if index != 0:
                    raise BackendError(f"{self.host} did not start the Junos CLI.", detail="Nothing was changed. Check its console.")
                self.entered_cli = True
            self._configure()
            return {"mgmt_ip": self._dhcp()}
        finally:
            self._leave()

    def _probe(self) -> int:
        for _ in range(3):
            self.session.send("")
            index, _ = self.session.expect(self.PROBE, 5)
            if index == 4:
                index, _ = self.session.expect(self.PROBE, 5)
            if index in (0, 1, 2):
                return index
            if index == 3:
                raise BackendError(
                    f"{self.host} is sitting in configuration mode.",
                    detail="Someone is mid-change on its console. Commit or exit there first; LabFront typed nothing into it.",
                )
        raise BackendError(
            f"{self.host} did not answer on its serial console.",
            detail="It may still be booting (vJunos takes about 5 minutes), or a qm terminal is holding the console.",
        )

    def _login(self) -> int:
        self.session.send("root")
        sent = False
        while True:
            index, _ = self.session.expect([SHELL, CLI, PASSWORD, INCORRECT, LOGIN], 30)
            if index == 0 or index == 1:
                self.logged_in = True
                return 1 if index == 0 else 2
            if index == 2 and not sent:
                self.session.send(self.password)
                sent = True
                continue
            if index == -1:
                raise BackendError(f"{self.host} did not finish logging in.", detail="Nothing was changed. Check its console.")
            raise BackendError(
                f"{self.host} has a password LabFront doesn't know.",
                detail="Its root password is neither empty nor this sandbox's password (see Reveal). "
                "LabFront changed nothing.",
            )

    def _configure(self) -> None:
        self.session.send("configure")
        index, _ = self.session.expect([CONFIG, CLI], 30)
        if index != 0:
            raise BackendError(
                f"{self.host} did not enter configuration mode.",
                detail="Another session may hold the configuration lock. Nothing was changed.",
            )
        try:
            self._step("delete chassis auto-image-upgrade", "removing auto-image-upgrade", check=False)
            self._root_password()
            self._step("set system services ssh root-login allow", "SSH root login")
            self._step(f"set system host-name {self.host}", "the host name")
            self._step("set interfaces fxp0 unit 0 family inet dhcp", "DHCP on fxp0")
            for number, line in enumerate(self.lines, 1):
                self._step(line, f"Mist adoption line {number} of {len(self.lines)}")
            self._commit()
        except _Refused as refused:
            self._rollback(refused)

    def _step(self, command: str, label: str, *, check: bool = True) -> None:
        self.session.send(command)
        index, text = self.session.expect([CONFIG], 30)
        if index != 0:
            raise _Refused(label, at_prompt=False, silent=True)
        if check and ERROR.search(_after_echo(text)):
            raise _Refused(label)

    def _root_password(self) -> None:
        label = "the root password"
        self.session.send("set system root-authentication plain-text-password")
        index, _ = self.session.expect([NEW_PASSWORD, CONFIG], 30)
        if index != 0:
            raise _Refused(label, at_prompt=index == 1, silent=index == -1)
        self.session.send(self.password)
        index, _ = self.session.expect([RETYPE, NEW_PASSWORD, CONFIG], 30)
        if index != 0:
            raise _Refused(label, at_prompt=index == 2, silent=index == -1)
        self.session.send(self.password)
        index, text = self.session.expect([CONFIG, NEW_PASSWORD], 30)
        if index != 0:
            raise _Refused(label, at_prompt=False, silent=index == -1)
        if ERROR.search(_after_echo(text)):
            raise _Refused(label)

    def _commit(self) -> None:
        self.session.send("commit and-quit")
        index, _ = self.session.expect([CLI, CONFIG], 180)
        if index == 1:
            raise _Refused("the commit")
        if index == -1:
            self.stuck = True
            raise BackendError(
                f"{self.host} did not finish the commit.",
                detail="It may still finish; check the console before adopting again. LabFront rolled nothing back.",
            )

    def _rollback(self, refused: _Refused) -> None:
        verb = f"stopped answering at {refused.label}" if refused.silent else f"refused {refused.label}"
        message = f"{self.host} {verb}."
        try:
            if not refused.at_prompt:
                self.session.interrupt()
                if self.session.expect([CONFIG], 10)[0] != 0:
                    raise _Refused("rollback")
            self.session.send("rollback 0")
            if self.session.expect([CONFIG], 60)[0] != 0:
                raise _Refused("rollback")
            self.session.send("exit")
            index, _ = self.session.expect([CLI, DISCARD, CONFIG], 30)
            if index == 1:
                self.session.send("yes")
                index, _ = self.session.expect([CLI], 30)
            if index != 0:
                raise _Refused("rollback")
        except (_Refused, BackendError):
            self.stuck = True
            raise BackendError(
                message,
                detail="LabFront could not roll it back; open the console, run rollback 0 and exit.",
            ) from None
        raise BackendError(message, detail="Nothing was committed: LabFront rolled the change back.")

    def _dhcp(self) -> str | None:
        for attempt in range(self.tries):
            if attempt:
                self.port.sleep(self.wait)
            self.session.send("show interfaces terse fxp0.0")
            _, text = self.session.expect([CLI], 30)
            found = INET.search(text)
            if found:
                return found.group(1)
        return None

    def _leave(self) -> None:
        """Log out if LabFront logged in; leave a console it found open as it was."""
        if self.stuck:
            return
        try:
            if self.logged_in:
                for _ in range(3):
                    self.session.send("exit")
                    index, _ = self.session.expect([LOGIN, SHELL, CLI, DISCARD], 10)
                    if index == 3:
                        self.session.send("yes")
                        index, _ = self.session.expect([LOGIN, SHELL, CLI], 10)
                    if index in (0, -1):
                        return
            elif self.entered_cli:
                self.session.send("exit")
                self.session.expect([SHELL, LOGIN, CLI], 10)
        except BackendError:
            pass


def adopt(port, host: str, password: str, lines: list[str], *, tries: int = 3, wait: float = 5.0) -> dict:
    """Join a vJunos to a Mist site over its console. Returns {"mgmt_ip": ip or None}.

    Logs in as root (no password on a fresh switch, else ``password``), sets the
    root password, SSH root login, the host name and DHCP on fxp0, then the Mist
    lines, and commits once. A refused line is rolled back so nothing half-done
    is left behind. Raises BackendError, never with console text in it.
    """
    return _Adoption(port, host, password, list(lines), tries=tries, wait=wait).run()
