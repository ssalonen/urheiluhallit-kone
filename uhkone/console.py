"""Human controls for a visible-browser booking session.

The input thread only reads terminal lines. Browser operations, prompts and
control decisions stay in the caller's thread through ``checkpoint`` and
``before_action``. This module writes no files and deliberately never renders
backend objects, error messages, account names, card data or reservation IDs.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import math
import os
from queue import Empty, Queue
import select
import sys
from threading import Event, Thread
import time
from typing import Callable, TextIO

from .models import Course


class StopRequested(KeyboardInterrupt):
    """The operator stopped the run; existing booking journals must survive."""


_PHASES = {
    "waiting": "Odotan ilmoittautumisen avautumista tai vapaata paikkaa.",
    "candidate": "Sopiva kurssi löytyi. Tarkistan varauksen tilanteen.",
    "adding": "Yritän varata kurssipaikan valitulle osallistujalle.",
    "held": "Keskeneräinen varaus löytyi. Muut kurssipolut pysyvät lukittuina.",
    "reservation_pending": "Selvitän varauspyynnön tulosta. Pyyntöä ei lähetetä uudelleen.",
    "paying": "Vien valitun varauksen maksuun.",
    "payment_pending": "Selvitän maksun tulosta. Maksua ei lähetetä uudelleen.",
    "queue_phase": "Tarkistan sopivat jonot.",
    "queue_pending": "Selvitän jonopyynnön tulosta. Tätä pyyntöä ei lähetetä uudelleen; muiden kurssien haku jatkuu.",
    "queueing": "Yritän varata yhden jonopaikan.",
    "queued": "Jonopaikka on vahvistettu. Se ei ole varsinainen kurssipaikka.",
    "confirmed": "Kurssivaraus on vahvistettu.",
    "deadline": "Yritysaika päättyi. Uusia varauksia tai maksuja ei aloiteta.",
    "retry": "Palvelu ei vastannut normaalisti. Odotan ennen uutta tarkistusta.",
    "working": "Varausajo on käynnissä.",
}
_ACTIONS = {
    "add": ("adding", "varaa kurssipaikka"),
    "checkout": ("paying", "jatka varauksen maksuun"),
    "queue": ("queueing", "varaa jonopaikka"),
}
_COMMANDS = {
    "tila": "status", "t": "status", "status": "status",
    "tauko": "pause", "p": "pause", "pause": "pause",
    "jatka": "resume", "j": "resume", "resume": "resume",
    "lopeta": "stop", "l": "stop", "q": "stop", "stop": "stop",
    "apu": "help", "a": "help", "?": "help", "help": "help",
}
_YES = {"kyllä", "kylla", "k", "yes", "y"}
_NO = {"ei", "e", "no", "n"}
_INPUT_ENDED = object()
_HELP = "Ohjaus + Enter: tila (t), tauko (p), jatka (j), lopeta (q), apu (?)."


def _public_text(value: str, limit: int) -> str:
    # Do not let public catalogue text inject terminal control sequences or
    # extra log lines. Raw descriptions and backend messages are never used.
    return " ".join("".join(char for char in value if char.isprintable()).split())[:limit]


def _course_label(course: Course | None) -> str:
    if course is None:
        return ""
    parts = [f"Kurssi #{course.id}: {_public_text(course.name, 120)}"]
    if course.location:
        parts.append(_public_text(course.location, 60))
    if course.start is not None:
        parts.append(course.start.strftime("%d.%m.%Y"))
    if course.time_start:
        parts.append(_public_text(course.time_start, 8))
    if course.price is not None and course.price.is_finite():
        parts.append(f"{course.price:.2f} €")
    return " · ".join(parts)


class TerminalSession:
    """Timestamped status and cooperative controls for the main browser thread.

    ``event`` accepts only phase codes and a public catalogue ``Course``;
    unrecognized phase codes receive a generic message and are never echoed.
    Pass the backend's event-pumping ``wait`` to control methods. No browser
    object or customer information is passed to the input thread.

    Existing noninteractive commands can use this class without a reader or
    prompts. ``ask_before_actions=True`` requires an interactive terminal.
    Tests may inject commands with ``submit_command`` and ``start_reader=False``.
    """

    def __init__(
        self, *, stream: TextIO | None = None, input_stream: TextIO | None = None,
        interactive: bool | None = None, ask_before_actions: bool = False,
        heartbeat_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None, start_reader: bool = True,
    ):
        if not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
            raise ValueError("Tilapäivitysten välin on oltava positiivinen.")
        self.stream = sys.stdout if stream is None else stream
        self.input_stream = sys.stdin if input_stream is None else input_stream
        self.interactive = bool(self.input_stream.isatty()) if interactive is None else interactive
        if ask_before_actions and not self.interactive:
            raise ValueError("Vaiheittainen hyväksyntä tarvitsee vuorovaikutteisen päätteen.")
        self.ask_before_actions = ask_before_actions
        self.heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now().astimezone())
        self._start_reader = start_reader
        self._commands: Queue = Queue()
        self._stop_reader = Event()
        self._reader: Thread | None = None
        self._started = False
        self._input_open = self.interactive
        self._paused = False
        self._stopped = False
        self._phase = "working"
        self._course: Course | None = None
        self._last_status: tuple | None = None
        self._last_output: float | None = None

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def input_open(self) -> bool:
        return self._input_open

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.interactive:
            self._line(_HELP)
            if self._start_reader:
                self._reader = Thread(target=self._read_lines, name="uhkone-terminal", daemon=True)
                self._reader.start()

    def close(self) -> None:
        self._stop_reader.set()
        if self._reader is not None:
            self._reader.join(timeout=0.5)
        # Never close the process's stdin. In particular, do not alter its
        # terminal mode or leak control-thread exceptions into browser logic.

    def submit_command(self, line: str) -> None:
        """Queue a command without executing browser work in the reader thread."""
        if not isinstance(line, str):
            raise TypeError("Ohjauskomennon on oltava tekstiä.")
        self._commands.put(line.strip().casefold())

    def finish_input(self) -> None:
        """Mark EOF once; a paused or approval-waiting run then stops safely."""
        self._commands.put(_INPUT_ENDED)

    def _read_lines(self) -> None:
        try:
            if os.name == "nt" and self.input_stream is sys.stdin:
                self._read_windows_console()
                return
            try:
                descriptor = self.input_stream.fileno()
            except (AttributeError, OSError, ValueError):
                descriptor = None
            while not self._stop_reader.is_set():
                if descriptor is not None:
                    # A cooked terminal is readable once an entire line (or
                    # EOF) is ready. Polling lets close() release stdin before
                    # another wizard prompt starts reading it.
                    ready, _, _ = select.select([descriptor], [], [], 0.2)
                    if not ready:
                        continue
                line = self.input_stream.readline()
                if not line:
                    self.finish_input()
                    return
                self.submit_command(line)
        except (OSError, ValueError):
            self.finish_input()

    def _read_windows_console(self) -> None:
        import msvcrt
        line: deque[str] = deque()
        while not self._stop_reader.wait(0.05):
            if not msvcrt.kbhit():
                continue
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()  # Function/arrow-key scan code.
            elif char in {"\r", "\n"}:
                msvcrt.putwch("\r")
                msvcrt.putwch("\n")
                self.submit_command("".join(line))
                line.clear()
            elif char == "\x03":
                self.submit_command("lopeta")
            elif char == "\x1a":
                self.finish_input()
                return
            elif char == "\b":
                if line:
                    line.pop()
                    for erase in "\b \b":
                        msvcrt.putwch(erase)
            elif char.isprintable():
                line.append(char)
                msvcrt.putwch(char)

    def _line(self, message: str) -> None:
        timestamp = self._wall_clock().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}", file=self.stream, flush=True)

    def event(self, phase: str, course: Course | None = None) -> None:
        if phase != "heartbeat":
            self._phase = phase if phase in _PHASES else "working"
            self._course = course
        self._status()

    def _status(self, *, force: bool = False) -> None:
        label = _course_label(self._course)
        status = (self._phase, label, self._paused)
        now = self._clock()
        if (not force and status == self._last_status and self._last_output is not None
                and now - self._last_output < self.heartbeat_seconds):
            return
        message = "Tauolla. Kirjoita jatka tai lopeta." if self._paused else _PHASES[self._phase]
        if label:
            message += " " + label
        self._line(message)
        self._last_status = status
        self._last_output = now

    def _raise_if_stopped(self) -> None:
        if self._stopped:
            raise StopRequested("Käyttäjä keskeytti varausajon.")

    def _stop(self, *, declined: bool = False) -> None:
        if not self._stopped:
            self._line(
                "Toimintoa ei hyväksytty. Pysäytän ajon; tallennettu varaustila säilyy."
                if declined else
                "Pysäytän ajon. Jo lähetettyä varausta tai maksua ei peruta; tallennettu tila säilyy."
            )
        self._stopped = True

    def _drain_commands(self, *, decision: bool = False) -> bool | None:
        answer = None
        while True:
            try:
                value = self._commands.get_nowait()
            except Empty:
                return answer
            if value is _INPUT_ENDED:
                if self._input_open:
                    self._input_open = False
                    self._line("Päätteen syöte sulkeutui. Ohjauskomentoja ei enää lueta.")
                continue
            if not value:
                continue
            command = _COMMANDS.get(value)
            if command == "stop":
                self._stop()
                answer = None
            elif command == "pause":
                self._paused = True
                self._status(force=True)
            elif command == "resume":
                self._paused = False
                self._status(force=True)
            elif command == "status":
                self._status(force=True)
            elif command == "help":
                self._line(_HELP)
            elif decision and value in _YES | _NO:
                if value in _NO:
                    answer = False
                elif self._paused:
                    self._line("Ajo on tauolla. Kirjoita ensin jatka, sitten hyväksyntä.")
                else:
                    answer = True
            else:
                # Never echo arbitrary terminal input into logs. It may be a
                # pasted account name, password, or card detail by mistake.
                self._line("Tuntematon komento. Kirjoita apu.")

    def checkpoint(self, wait: Callable[[float], None]) -> None:
        """Handle controls before new work; keep browser events alive on pause."""
        self._drain_commands()
        self._raise_if_stopped()
        while self._paused:
            if not self._input_open:
                self._stop()
                self._raise_if_stopped()
            self._status()
            wait(0.2)
            self._drain_commands()
            self._raise_if_stopped()
        self._status()

    def before_action(
        self, action: str, course: Course | None = None, *, wait: Callable[[float], None],
    ) -> None:
        """Authorize one write, optionally asking through the same input queue."""
        if action not in _ACTIONS:
            raise ValueError("Tuntematon vahvistettava toiminto.")
        self.checkpoint(wait)
        phase, label = _ACTIONS[action]
        self.event(phase, course)
        if not self.ask_before_actions:
            return
        self._line(f"Hyväksytkö seuraavan toiminnon: {label}? Kirjoita kyllä tai ei + Enter.")
        while True:
            answer = self._drain_commands(decision=True)
            self._raise_if_stopped()
            if answer is False:
                self._stop(declined=True)
                self._raise_if_stopped()
            if answer is True and not self._paused:
                self._line("Toiminto hyväksytty.")
                return
            if not self._input_open:
                self._stop()
                self._raise_if_stopped()
            self._status()
            wait(0.2)
