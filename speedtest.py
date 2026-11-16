#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import os
import sys
import threading
import time
from http.client import HTTPException
from typing import Callable, NamedTuple, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHUNK_SIZE = 64 * 1024
USER_AGENT = "speedtest-cli/1.0"

SPINNER_FRAMES = "|/-\\"
SPINNER_INTERVAL = 0.1

GREEN = "32"
BRIGHT = "1;92"
DIM = "2;32"
RED = "1;31"
CLEAR_LINE = "\r\033[K"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


class FetchResult(NamedTuple):
    nbytes: int
    seconds: float


class Summary(NamedTuple):
    successful: int
    total: int
    total_bytes: int
    avg_time: float
    mbit_s: float
    mb_s: float


def fetch_once(
    url: str, timeout: float, on_progress: Callable[[int], None] | None = None
) -> FetchResult:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    started = time.perf_counter()
    with urlopen(request, timeout=timeout) as response:
        total = 0
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if on_progress is not None:
                on_progress(total)
    return FetchResult(total, time.perf_counter() - started)


def describe_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    reason = exc.reason if isinstance(exc, URLError) else exc
    return str(reason) or type(exc).__name__


def to_mbit_s(nbytes: int, seconds: float) -> float:
    return nbytes * 8 / seconds / 1_000_000 if seconds > 0 else 0.0


def summarize(results: list[FetchResult], total: int) -> Summary | None:
    if not results:
        return None
    total_bytes = sum(r.nbytes for r in results)
    total_time = sum(r.seconds for r in results)
    mbit_s = to_mbit_s(total_bytes, total_time)
    return Summary(
        successful=len(results),
        total=total,
        total_bytes=total_bytes,
        avg_time=total_time / len(results),
        mbit_s=mbit_s,
        mb_s=mbit_s / 8,
    )


def use_color(stream: TextIO) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def paint(text: str, code: str, stream: TextIO = sys.stdout) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color(stream) else text


class Spinner:
    def __init__(self, label: str, stream: TextIO = sys.stdout) -> None:
        self.label = label
        self.stream = stream
        self.enabled = use_color(stream)
        self.nbytes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def update(self, nbytes: int) -> None:
        self.nbytes = nbytes

    def _render(self, frame: str, elapsed: float) -> None:
        self.stream.write(
            f"{CLEAR_LINE}{paint(f'[{frame}]', BRIGHT)} {self.label}  "
            f"{paint(f'{self.nbytes / 1_000_000:.2f} MB  {elapsed:.1f} s', DIM)}"
        )
        self.stream.flush()

    def _spin(self) -> None:
        started = time.perf_counter()
        for frame in itertools.cycle(SPINNER_FRAMES):
            self._render(frame, time.perf_counter() - started)
            if self._stop.wait(SPINNER_INTERVAL):
                break

    def __enter__(self) -> Spinner:
        if self.enabled:
            self.stream.write(HIDE_CURSOR)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.enabled:
            self._stop.set()
            self._thread.join()
            self.stream.write(CLEAR_LINE + SHOW_CURSOR)
            self.stream.flush()


def format_bytes(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_row(marker: str, key: str, value: str) -> str:
    return f"{paint(marker, BRIGHT)} {paint(f'{key:<11}', DIM)} {value}"


def format_banner(url: str, count: int, timeout: float) -> str:
    return "\n".join(
        [
            paint(">> speedtest :: bandwidth probe", BRIGHT),
            paint(">> " + "-" * 30, DIM),
            format_row("[*]", "target", paint(url, GREEN)),
            format_row("[*]", "requests", paint(str(count), GREEN)),
            format_row("[*]", "timeout", paint(f"{timeout:g} s", GREEN)),
            "",
        ]
    )


def format_report(summary: Summary) -> str:
    mb = summary.total_bytes / 1_000_000
    return "\n".join(
        [
            format_row("[=]", "successful", paint(f"{summary.successful}/{summary.total}", GREEN)),
            format_row(
                "[=]",
                "downloaded",
                paint(f"{mb:.2f} MB ({format_bytes(summary.total_bytes)} bytes)", GREEN),
            ),
            format_row("[=]", "avg time", paint(f"{summary.avg_time:.3f} s", GREEN)),
            format_row(
                "[=]",
                "speed",
                paint(f"{summary.mbit_s:.2f} Mbit/s", BRIGHT)
                + paint(f" ({summary.mb_s:.2f} MB/s)", GREEN),
            ),
        ]
    )


def print_success(label: str, result: FetchResult) -> None:
    print(
        f"{paint('[+]', BRIGHT)} {label}  "
        f"{paint(f'{result.nbytes / 1_000_000:.2f} MB', GREEN)}  "
        f"{paint(f'{result.seconds:.3f} s', GREEN)}  "
        f"{paint(f'{to_mbit_s(result.nbytes, result.seconds):.2f} Mbit/s', BRIGHT)}",
        flush=True,
    )


def print_failure(label: str, msg: str) -> None:
    print(
        f"{paint('[x]', RED, sys.stderr)} {label}  {paint(msg, RED, sys.stderr)}",
        file=sys.stderr,
        flush=True,
    )


def print_fatal(msg: str) -> None:
    print(paint(f"[!] {msg}", RED, sys.stderr), file=sys.stderr, flush=True)


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return n


def positive_float(value: str) -> float:
    x = float(value)
    if x <= 0:
        raise argparse.ArgumentTypeError("must be a float > 0")
    return x


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure sequential download speed for a given URL.",
    )
    parser.add_argument("url", help="HTTP(S) URL to download")
    parser.add_argument(
        "-n",
        "--count",
        type=positive_int,
        default=10,
        help="number of sequential requests (default: 10)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=30.0,
        help="request timeout in seconds (default: 30)",
    )
    return parser.parse_args(argv)


def validate_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return "URL must start with http:// or https://"
    if not parsed.netloc:
        return "URL must contain a host"
    return None


def run(url: str, count: int, timeout: float) -> list[FetchResult]:
    results: list[FetchResult] = []
    width = len(str(count))
    for i in range(1, count + 1):
        label = f"{i:0{width}}/{count}"
        try:
            with Spinner(label) as spinner:
                result = fetch_once(url, timeout, spinner.update)
        except (OSError, HTTPException) as exc:
            print_failure(label, describe_error(exc))
            continue
        results.append(result)
        print_success(label, result)
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    error = validate_url(args.url)
    if error is not None:
        print_fatal(error)
        return 2

    print(format_banner(args.url, args.count, args.timeout), flush=True)

    try:
        results = run(args.url, args.count, args.timeout)
    except KeyboardInterrupt:
        print_fatal("interrupted")
        return 130

    summary = summarize(results, args.count)
    if summary is None:
        print_fatal("all requests failed")
        return 1

    print(flush=True)
    print(format_report(summary), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
