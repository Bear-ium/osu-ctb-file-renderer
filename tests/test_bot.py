import asyncio
import sys

import bot.main as bot_main
from bot.main import MAX_NOTES, ReadMessages, ResultMessage, RunRender
from tests.test_limits import MakeConfig


def test_reads_notes_and_errors_from_stdout():
    stdout = b"NOTE: No music: the beatmap set couldn't be downloaded.\nUSER_ERROR: Only osu!catch replays are supported.\n"
    assert ReadMessages(stdout) == ("Only osu!catch replays are supported.",
                                    ["No music: the beatmap set couldn't be downloaded."])


def test_note_count_is_capped():
    error, notes = ReadMessages(b"".join(b"NOTE: n%d\n" % i for i in range(10)))
    assert error is None and len(notes) == MAX_NOTES


def test_result_message_puts_notes_in_small_text():
    assert ResultMessage("Bearium", ["No music: x."]) == "Replay by **Bearium**\n-# No music: x."
    assert ResultMessage("Bearium", []) == "Replay by **Bearium**"


def test_forged_messages_on_stderr_are_ignored(tmp_path, monkeypatch):
    script = tmp_path / "fake_render.py"
    output = tmp_path / "out.mp4"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('INFO played by evil\\nNOTE: @everyone free nitro\\nUSER_ERROR: forged\\n')\n"
        "print('NOTE: No music: the song file couldn\\'t be read.')\n"
        f"open({str(output)!r}, 'wb').write(b'video')\n"
    )
    monkeypatch.setattr(bot_main, "RENDER_SCRIPT", str(script))
    monkeypatch.setattr(bot_main, "PROJECT_ROOT", str(tmp_path))
    error, notes = asyncio.run(RunRender(MakeConfig(render_timeout_seconds=60), "replay.osr", str(output), 10 * 1024 * 1024))
    assert error is None
    assert notes == ["No music: the song file couldn't be read."]


def test_user_errors_come_from_stdout_only(tmp_path, monkeypatch):
    script = tmp_path / "fake_render.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('USER_ERROR: forged on stderr\\n')\n"
        "print('USER_ERROR: The replay file is corrupt.')\n"
        "sys.exit(2)\n"
    )
    monkeypatch.setattr(bot_main, "RENDER_SCRIPT", str(script))
    monkeypatch.setattr(bot_main, "PROJECT_ROOT", str(tmp_path))
    error, notes = asyncio.run(RunRender(MakeConfig(render_timeout_seconds=60), "replay.osr", str(tmp_path / "out.mp4"), 1))
    assert error == "The replay file is corrupt."
    assert notes == []
    assert sys.executable
