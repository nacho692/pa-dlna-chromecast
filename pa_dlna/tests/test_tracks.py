"""Tests playing tracks with upmpdcli and mpd."""

# XXX
# requires_resources

import sys
import os
import time
import asyncio
import tempfile
import pathlib
import signal
import subprocess
from contextlib import asynccontextmanager, AsyncExitStack
from textwrap import dedent

from ..init import parse_args
from ..config import UserConfig
from ..pa_dlna import AVControlPoint

import logging
logger = logging.getLogger('tsample')

# Courtesy of https://espressif-docs.readthedocs-hosted.com/projects/esp-adf/
# en/latest/design-guide/audio-samples.html.
TRACK_PATH = pathlib.Path(__file__).parent / 'gs-16b-1c-44100hz.mp3'
TRACK_NAME = TRACK_PATH.stem

class TrackRuntimeError(Exception): pass

@asynccontextmanager
async def create_config_home(sink_name):
    "Yield temporary directory to be used as the value of XDG_CONFIG_HOME"

    with tempfile.TemporaryDirectory(dir='.') as tmpdirname:
        dirpath = pathlib.Path(tmpdirname) / 'mpd'
        dirpath.mkdir()
        state_path = dirpath / 'state'
        with open(state_path, 'w'):
            pass
        sticker_path = dirpath / 'sticker.sql'
        with open(sticker_path, 'w'):
            pass
        mpd_conf = dirpath / 'mpd.conf'
        with open(mpd_conf, 'w') as f:
            f.write(dedent(f'''\
                        state_file      "{state_path}"
                        sticker_file    "{sticker_path}"

                        audio_output {{
                            type            "pulse"
                            name            "My Pulse Output"
                            sink            "{sink_name}"
                        }}
                        '''))
        yield tmpdirname

@asynccontextmanager
async def run_control_point(config_home, loglevel):
    async def cp_connected():
        # Wait for the connection to LibPulse.
        while cp.pulse is None or cp.pulse.lib_pulse is None:
            await asyncio.sleep(0)
        logger.debug('Connected to libpulse')
        return  cp

    argv = ['--loglevel', loglevel]
    options, _ = parse_args('pa-dlna sample tests', argv=argv)

    # Override any existing pa-dlna user configuration with no user
    # configuration.
    _environ = os.environ.copy()
    try:
        os.environ.update({'XDG_CONFIG_HOME': config_home})
        config = UserConfig()
    finally:
        os.environ.clear()
        os.environ.update(_environ)
    cp = AVControlPoint(config=config, **options)
    asyncio.create_task(cp.run_control_point())

    try:
        yield await asyncio.wait_for(cp_connected(), 5)
    except TimeoutError:
        raise TrackRuntimeError('Cannot connect to libpulse') from None
    finally:
        await cp.close()

async def proc_terminate(proc, signal=None, timeout=0.2):
    async def _terminate(funcname, delay):
        start = time.monotonic()
        if funcname == 'send_signal':
            proc.send_signal(signal)
        else:
            getattr(proc, funcname)()
        await asyncio.sleep(0)
        while proc.returncode is None and time.monotonic() - start < delay:
            await asyncio.sleep(0)

    if proc.returncode is None:
        if signal is not None:
            await _terminate('send_signal', timeout)
        else:
            await _terminate('terminate', timeout)
        if proc.returncode is None:
            await _terminate('kill', 0)

@asynccontextmanager
async def proc_run(cmd, *args, env=None):
    logger.debug(f"Run command '{cmd} {' '.join(args)}'")
    environ = None
    if env is not None:
        environ = os.environ.copy()
        environ.update(env)
    proc = await asyncio.create_subprocess_exec(
        cmd, *args, env=environ,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        stderr = await asyncio.wait_for(proc.stderr.readline(), 5)
    except TimeoutError:
        raise TrackRuntimeError(
            f"'{cmd!r}' failure to output first stderr line") from None

    yield proc

    if cmd == 'upmpdcli':
        await proc_terminate(proc, signal=signal.SIGINT)
    else:
        await proc_terminate(proc)
    await proc.wait()

    if logger.getEffectiveLevel() == logging.DEBUG:
        logger.debug(f'[{cmd!r} exited with {proc.returncode}]')
        stdout = await proc.stdout.read()
        if stdout:
            logger.debug(f'[stdout]\n{stdout.decode()}')
        stderr += await proc.stderr.read()
        if stderr:
            logger.debug(f'[stderr]\n{stderr.decode()}')

async def play_track_ffmpeg(upmpdcli, path, name):
        args = ['-hide_banner', '-nostats', '-i', path, '-f', 'pulse',
                '-device', str(upmpdcli.renderer.nullsink.sink.index), name]
        return await upmpdcli.exit_stack.enter_async_context(
                                                proc_run('ffmpeg', *args))

class UpmpdcliMpd:
    """Set up the environment to play tracks with upmpdcli and mpd.

    'upmpdcli' is a DLNA Media Renderer implementation that forwards audio
    to 'mpd' and 'mpd' is configured to output audio to a pulse sink.

    The UpmpdcliMpd instance starts both processes, creates the pulse sink
    used by 'mpd' and gets the upmpdcli pa-dlna renderer.

    UpmpdcliMpd must be instantiated in an 'async with' statement.
    """

    def __init__(self, mpd_sink_name='MPD-sink', loglevel='info'):
        self.mpd_sink_name = mpd_sink_name
        self.loglevel = loglevel
        self.mpd_sink = None
        self.control_point = None
        self.lib_pulse = None
        self.renderer = None
        self.exit_stack = AsyncExitStack()

    @asynccontextmanager
    async def create_sink(self):
        logger.debug(f"Create sink '{self.mpd_sink_name}'")
        module_index = await self.lib_pulse.pa_context_load_module(
            'module-null-sink',
            f'sink_name="{self.mpd_sink_name}" '
            f'sink_properties=device.description="{self.mpd_sink_name}"')
        try:
            for sink in await self.lib_pulse.pa_context_get_sink_info_list():
                if sink.owner_module == module_index:
                    yield sink
                    break
            else:
                raise TrackRuntimeError(
                    f"Cannot find sink '{self.mpd_sink_name}'") from None
        finally:
            await self.lib_pulse.pa_context_unload_module(module_index)

    async def get_renderer(self):
        renderer = None
        while renderer is None:
            for rndrer in self.control_point.renderers():
                if rndrer.name.startswith('UpMPD-'):
                    renderer = rndrer
                    break
            await asyncio.sleep(0)

        while renderer.encoder is None:
            await asyncio.sleep(0)

        # Make sure the control_point is idle.
        for i in range(10):
            await asyncio.sleep(0)

        logger.debug(f'Found renderer {renderer.name}')
        return renderer

    async def __aenter__(self):
        try:
            # Run the AVControlPoint.
            config_home = await self.exit_stack.enter_async_context(
                                    create_config_home(self.mpd_sink_name))
            self.control_point = await self.exit_stack.enter_async_context(
                                    run_control_point(config_home,
                                                      self.loglevel))
            self.lib_pulse = self.control_point.pulse.lib_pulse
            logger.debug(f"XDG_CONFIG_HOME is '{config_home}'")

            # Create 'MPD-sink'.
            self.mpd_sink = await self.exit_stack.enter_async_context(
                                    self.create_sink())

            # Start the mpd and upmpdcli processes.
            await self.exit_stack.enter_async_context(
                                    proc_run('mpd', '--no-daemon',
                                        env={'XDG_CONFIG_HOME': config_home}))
            await self.exit_stack.enter_async_context(
                                    proc_run('upmpdcli', '-i', 'lo'))

            # Get the pa-dlna Renderer instance of the upmpdcli DLNA device.
            try:
                self.renderer = await asyncio.wait_for(self.get_renderer(), 5)
            except TimeoutError:
                raise TrackRuntimeError(
                    'Cannot find the upmpdcli Renderer instance') from None

            return self

        except Exception as e:
            await self.exit_stack.aclose()
            if isinstance(e, TrackRuntimeError):
                sys.exit(f'*** error: {e!r}')
            else:
                raise

    async def __aexit__(self, exc_type, exc_value, traceback):
        # Prevent the upmpdcli process to send a new SSDP notification while
        # we are closing the control point to avoid harmless and annoying
        # logs.
        await self.control_point.close()

        await self.exit_stack.aclose()

async def main():
    async with UpmpdcliMpd(loglevel='debug') as upmpdcli:
        # Play a track.
        ffmpeg_proc = await play_track_ffmpeg(upmpdcli, str(TRACK_PATH),
                                                                TRACK_NAME)

        # XXX
        await asyncio.sleep(20)

if __name__ == '__main__':
    asyncio.run(main())
