"""Whether a recording carries narration.

This is the first gate in the workflow. Checkpoint 0 says that if the probe reports a silent
track, ask for a re-record before doing anything else — so a false "silent" costs the user a
re-recording they did not need, and a false "has audio" sends the agent looking for expected
results that were never spoken.

The verdict used to be driven by counting `silencedetect` events, which is the *inverse* of
speech: each event marks the end of a silent stretch. A track that is loud from the first frame
to the last emits none at all, scored zero, and was reported NEARLY SILENT — the loudest possible
recording classified as the quietest. It survived because narration with ordinary pauses does
generate plenty of events, which is the single case it was built against.

Tested through `audio_verdict` rather than the CLI so these need no ffmpeg and no media file.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_media as pm  # noqa: E402

FLOOR = -45.0


def verdict(peak, mean, silences=0):
    return pm.audio_verdict(peak_db=peak, mean_db=mean, silences=silences, threshold_db=FLOOR)


# --- the regression that started this -----------------------------------------------------------
def test_continuous_speech_with_no_pauses_is_audio_present():
    """Zero pauses means uninterrupted sound, not silence.

    A 300 Hz tone at mean -21 dB produces no silencedetect events at all. Under the old
    pause-counting rule that scored 0 and came back NEARLY SILENT.
    """
    assert verdict(peak=-12.8, mean=-21.1, silences=0).startswith("AUDIO PRESENT")


def test_narration_with_many_pauses_is_also_audio_present():
    """The case the old rule got right, which must keep working."""
    assert verdict(peak=-6.0, mean=-28.0, silences=37).startswith("AUDIO PRESENT")


@pytest.mark.parametrize("silences", [0, 1, 2, 3, 50])
def test_the_pause_count_never_changes_the_verdict(silences):
    """Loudness decides. Pauses are description, not evidence."""
    assert verdict(peak=-10.0, mean=-20.0, silences=silences).startswith("AUDIO PRESENT")
    assert verdict(peak=-90.0, mean=-90.0, silences=silences).startswith("SILENT")


# --- the quiet end ------------------------------------------------------------------------------
def test_a_truly_silent_track_is_silent():
    assert verdict(peak=-91.0, mean=-91.0).startswith("SILENT")


def test_a_single_chime_over_a_quiet_track_is_nearly_silent():
    """Loud peak, inaudible average: a notification sound, not someone talking."""
    result = verdict(peak=-9.0, mean=-62.0)
    assert result.startswith("NEARLY SILENT")
    assert "-62.0" in result and "-9.0" in result   # both numbers, so the reader can judge


def test_missing_measurements_are_not_read_as_audio():
    """ffmpeg not reporting a level is not evidence of narration."""
    assert verdict(peak=None, mean=None).startswith("SILENT")
    assert verdict(peak=-10.0, mean=None).startswith("NEARLY SILENT")


# --- the boundary -------------------------------------------------------------------------------
def test_exactly_at_the_floor_counts_as_audio():
    assert verdict(peak=-20.0, mean=FLOOR).startswith("AUDIO PRESENT")


def test_just_below_the_floor_does_not():
    assert verdict(peak=-20.0, mean=FLOOR - 0.1).startswith("NEARLY SILENT")


def test_the_threshold_is_honoured_rather_than_hardcoded():
    """A caller raising the floor must be able to make the same track fail it."""
    strict = pm.audio_verdict(peak_db=-12.0, mean_db=-21.0, silences=0, threshold_db=-10.0)
    assert strict.startswith("NEARLY SILENT")


def test_every_verdict_names_a_measured_number():
    """The reader has to be able to disagree with it, which needs the evidence in the sentence."""
    for peak, mean in ((-91.0, -91.0), (-9.0, -62.0), (-12.8, -21.1)):
        assert any(str(x) in verdict(peak, mean) for x in (peak, mean))
