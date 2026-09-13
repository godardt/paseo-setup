"""Masked token entry on supported POSIX terminals, without real credentials."""

import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install


class TerminalInput(io.StringIO):
    def fileno(self):
        return 123


class MaskedInputTests(unittest.TestCase):
    def read_input(self, text, expected_error=None, read_error=None, setup_error=None):
        incoming, outgoing = TerminalInput(text), io.StringIO()
        original = [0, 0, 0, termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG,
                    0, 0, [b"\x00"] * 32]
        with patch.object(install.sys, "stdin", incoming), \
             patch.object(install.sys, "stderr", outgoing), \
             patch.object(install.termios, "tcgetattr", return_value=original), \
             patch.object(install.termios, "tcsetattr") as set_attributes, \
             patch.object(incoming, "read", wraps=incoming.read) as read:
            if read_error:
                read.side_effect = read_error
            if setup_error:
                set_attributes.side_effect = [setup_error, None]
            if expected_error:
                with self.assertRaises(expected_error):
                    install.masked_input("Token: ")
                value = None
            else:
                value = install.masked_input("Token: ")
            if setup_error:
                read.assert_not_called()
        masked = set_attributes.call_args_list[0].args[2]
        self.assertFalse(masked[3] & (termios.ECHO | termios.ECHONL | termios.ICANON))
        self.assertTrue(masked[3] & termios.ISIG)
        self.assertEqual(masked[6][termios.VMIN], 1)
        self.assertEqual(masked[6][termios.VTIME], 0)
        self.assertEqual(original[6], [b"\x00"] * 32)
        set_attributes.assert_called_with(123, termios.TCSAFLUSH, original)
        return value, outgoing.getvalue()

    def test_masks_each_character_and_restores_terminal(self):
        token = "fixture-secret"
        value, output = self.read_input(token + "\n")
        self.assertEqual(value, token)
        self.assertEqual(output, "Token: " + "*" * len(token) + "\n")
        self.assertNotIn(token, output)

    def test_editing_and_empty_input(self):
        for text, expected in (("abX\x7fc\n", "abc"), ("\x08a\x08b\n", "b"),
                               ("oops\x15good\n", "good"), ("\n", ""), ("\r", "")):
            with self.subTest(text=text):
                value, output = self.read_input(text)
                self.assertEqual(value, expected)
                self.assertTrue(all(char in "*\b \n" for char in output.removeprefix("Token: ")))

    def test_eof_and_ctrl_c_restore_terminal(self):
        for text, error in (("", EOFError), ("partial", EOFError), ("partial\x04", EOFError),
                            ("partial\x03", KeyboardInterrupt)):
            with self.subTest(error=error, text=text):
                _, output = self.read_input(text, expected_error=error)
                self.assertNotIn("partial", output)

    def test_terminal_setup_failure_never_reads_or_echoes_token(self):
        _, output = self.read_input("fixture-secret\n", expected_error=termios.error,
                                    setup_error=termios.error("terminal unavailable"))
        self.assertEqual(output, "\n")

    def test_read_error_restores_terminal(self):
        _, output = self.read_input("fixture-secret\n", expected_error=OSError,
                                    read_error=OSError("read failed"))
        self.assertEqual(output, "Token: \n")

    def terminal_process(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        original = termios.tcgetattr(slave)
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import install\n"
            "try:\n"
            "    value = install.masked_input('Token: ')\n"
            "    print('MATCH' if value == 'fixture-secret' else 'MISMATCH', flush=True)\n"
            "except KeyboardInterrupt:\n"
            "    print('CANCELED', flush=True)\n"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(ROOT / "scripts")],
                                   stdin=slave, stdout=slave, stderr=slave)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        return process, master, slave, original

    def read_until(self, descriptor, marker):
        output = bytearray()
        deadline = time.monotonic() + 5
        while marker not in output:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "Timed out waiting for terminal output")
            ready, _, _ = select.select([descriptor], [], [], remaining)
            self.assertTrue(ready, "Timed out waiting for terminal output")
            data = os.read(descriptor, 4096)
            self.assertTrue(data, "Terminal closed before the expected output")
            output.extend(data)
        return bytes(output)

    def test_real_terminal_masks_paste_and_supports_backspace(self):
        process, master, slave, original = self.terminal_process()
        output = self.read_until(master, b"Token: ")
        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO)
        os.write(master, b"fixture-secret\x7ft\n")
        output += self.read_until(master, b"MATCH\r\n")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertNotIn(b"fixture-secret", output)
        self.assertNotIn(b"MISMATCH", output)
        self.assertEqual(output.count(b"*"), len("fixture-secret") + 1)
        self.assertIn(b"\b \b", output)
        self.assertEqual(termios.tcgetattr(slave), original)

    def test_real_terminal_restores_after_interrupt(self):
        process, master, slave, original = self.terminal_process()
        output = self.read_until(master, b"Token: ")
        os.write(master, b"partial")
        output += self.read_until(master, b"*" * len("partial"))
        process.send_signal(signal.SIGINT)
        output += self.read_until(master, b"CANCELED\r\n")
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertNotIn(b"partial", output)
        self.assertEqual(termios.tcgetattr(slave), original)


if __name__ == "__main__":
    unittest.main()
