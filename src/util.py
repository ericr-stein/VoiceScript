from pydub import AudioSegment
import subprocess
import asyncio
import os
import logging

logger = logging.getLogger(__name__)
DEVICE = os.getenv("DEVICE")


def isolate_voices(file_paths):
    for index in range(len(file_paths)):
        chunk_length_ms = 100
        chunked = []
        for file in file_paths:
            audio = AudioSegment.from_file(file)
            chunked.append([audio[i : i + chunk_length_ms] for i in range(0, len(audio), chunk_length_ms)])

        processed_chunks = [
            filter_nondominant_voice([chunks[i] for chunks in chunked], index) for i in range(len(chunked[0]))
        ]

        processed_audio = sum(processed_chunks)
        processed_audio.export(file_paths[index])


def filter_nondominant_voice(segments, index):
    value = segments[index].dBFS
    for i, segment in enumerate(segments):
        if i == index:
            continue
        if segment.dBFS > value:
            return segment - 100
    return segments[index]


async def get_length(filename):
    """Asynchronously get the duration of a media file using ffprobe."""
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filename,
    ]
    logger.debug(f"Running async ffprobe for: {filename}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_message = stderr.decode().strip()
        logger.error(f"ffprobe error for {filename}: {error_message}")
        return None  # Indicate failure

    try:
        duration = float(stdout.decode().strip())
        logger.debug(f"ffprobe success for {filename}, duration: {duration}")
        return duration
    except ValueError:
        logger.error(f"Could not parse ffprobe duration output for {filename}: {stdout.decode()}")
        return None  # Indicate failure


async def time_estimate(filename, online=True):
    """Asynchronously estimate processing time based on file duration."""
    try:
        # For now, we don't predict the wait time for zipped files in the queue.
        if filename.lower().endswith(".zip"):
            return 1, 1

        run_time = await get_length(filename)  # Await the async call

        if run_time is None:  # Handle ffprobe failure
            logger.warning(f"Failed to get length for {filename}, using default estimate.")
            # Return a default estimate or error indication
            return 60, -1  # e.g., 60s estimate, -1 runtime indicates error

        # Your existing estimation logic
        if online:
            if DEVICE == "mps":
                estimate = run_time / 5
            else:
                estimate = run_time / 10
        else:
            if DEVICE == "mps":
                estimate = run_time / 3
            else:
                estimate = run_time / 6
        return estimate, run_time

    except Exception as e:
        logger.exception(f"Error during time estimation for {filename}: {e}")
        return -1, -1  # Indicate error
